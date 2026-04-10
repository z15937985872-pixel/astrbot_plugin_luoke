from pathlib import Path
import random
import re
import asyncio
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .requests import Request
from .database import Database
from .utils import (
    format_elf_text, format_skill_text, format_team_text,
    format_egg_group_text, format_breeding_plan_text,
    split_long_message
)

# 配置默认值
DEFAULT_CONFIG = {
    "reply_mode": "image",
    "cache_ttl_hours": 24,
    "source_mode": "auto",
    "update_mode": "disabled",
    "merge_forward_enabled": True,
    "merge_forward_threshold": 900,
    "merge_forward_platforms": "aiocqhttp,onebot",
    "query_max_results": 8
}

@dataclass
class PendingSelection:
    keyword: str
    records: List[Dict]
    ts: float
    query_type: str  # "elf", "skill", "team", "egg", "breeding"

@register("roco_wiki", "Improved", "洛克王国 Wiki 图鉴助手（缓存+文本/图片双模式）", "3.0.0")
class RocoWiki(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.plugin_data_path = Path(get_astrbot_data_path()) / "plugin_data" / "roco_wiki"
        self.plugin_data_path.mkdir(parents=True, exist_ok=True)

        self.request = Request(self.plugin_data_path)
        self.db = Database(self.plugin_data_path)

        self._pending: Dict[str, PendingSelection] = {}
        self._pending_ttl = timedelta(minutes=10)
        self._background_tasks = set()
        self.elf_catalog = []
        self.skill_catalog = []

    # 配置读取
    def _get_config_str(self, key: str, default: str = "") -> str:
        val = self.config.get(key, default)
        return str(val).strip() if val is not None else default

    def _get_config_int(self, key: str, default: int) -> int:
        val = self.config.get(key, default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _get_config_bool(self, key: str, default: bool) -> bool:
        val = self.config.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes", "on")
        return bool(val)

    @property
    def reply_mode(self) -> str:
        mode = self._get_config_str("reply_mode", "image").lower()
        if mode not in ("image", "text", "hybrid"):
            return "image"
        return mode

    @property
    def cache_ttl_hours(self) -> int:
        return max(1, self._get_config_int("cache_ttl_hours", 24))

    @property
    def source_mode(self) -> str:
        mode = self._get_config_str("source_mode", "auto").lower()
        if mode not in ("auto", "crawler_only", "cache_only"):
            return "auto"
        return mode

    @property
    def update_mode(self) -> str:
        mode = self._get_config_str("update_mode", "disabled").lower()
        if mode not in ("disabled", "after_send"):
            return "disabled"
        return mode

    @property
    def merge_forward_enabled(self) -> bool:
        return self._get_config_bool("merge_forward_enabled", True)

    @property
    def merge_forward_threshold(self) -> int:
        return max(100, self._get_config_int("merge_forward_threshold", 900))

    @property
    def merge_forward_platforms(self) -> set:
        raw = self._get_config_str("merge_forward_platforms", "aiocqhttp,onebot")
        return {p.strip().lower() for p in raw.split(",") if p.strip()}

    @property
    def query_max_results(self) -> int:
        return max(1, min(20, self._get_config_int("query_max_results", 8)))

    def _supports_forward(self, event: AstrMessageEvent) -> bool:
        if not self.merge_forward_enabled:
            return False
        platform = (event.get_platform_name() or "").lower()
        return platform in self.merge_forward_platforms

    def _selection_key(self, event: AstrMessageEvent) -> str:
        return f"{event.get_session_id()}::{event.get_sender_id() or '-'}"

    def _pending_get(self, key: str) -> Optional[PendingSelection]:
        pending = self._pending.get(key)
        if not pending:
            return None
        if datetime.now() - pending.ts > self._pending_ttl:
            self._pending.pop(key, None)
            return None
        return pending

    def _pending_set(self, key: str, keyword: str, records: List[Dict], query_type: str):
        self._pending[key] = PendingSelection(
            keyword=keyword, records=records, ts=datetime.now(), query_type=query_type
        )

    def _pending_clear(self, key: str):
        self._pending.pop(key, None)

    async def _send_message(self, event: AstrMessageEvent, content: str):
        if not self._supports_forward(event) or len(content) <= self.merge_forward_threshold:
            await event.send(event.plain_result(content).stop_event())
            return
        # 合并转发
        nodes = []
        chunks = split_long_message(content, self.merge_forward_threshold)
        for chunk in chunks:
            nodes.append(Node(uin=event.get_self_id(), name="洛克百科", content=[Plain(chunk)]))
        result = event.make_result()
        result.chain.append(Nodes(nodes))
        await event.send(result.stop_event())

    async def initialize(self):
        logger.info("正在初始化洛克王国 Wiki 图鉴助手...")
        try:
            self.elf_catalog = await self.request.fetch_catalog("https://wiki.lcx.cab/lk/index.php")
            self.skill_catalog = await self.request.fetch_skill_catalog("https://wiki.lcx.cab/lk/skill_list.php")
            logger.info(f"精灵目录: {len(self.elf_catalog)} 只, 技能目录: {len(self.skill_catalog)} 个")
        except Exception as e:
            logger.error(f"加载目录失败: {e}")
            self.elf_catalog = []
            self.skill_catalog = []

    def _match_elf(self, query: str) -> List[Dict]:
        q = query.lower()
        matches = [i for i in self.elf_catalog if q in i["name"].lower()]
        matches.sort(key=lambda x: (0 if x["name"].lower() == q else 1 if x["name"].lower().startswith(q) else 2, len(x["name"])))
        return matches[:self.query_max_results]

    def _match_skill(self, query: str) -> List[Dict]:
        q = query.lower()
        matches = [i for i in self.skill_catalog if q in i["name"].lower()]
        matches.sort(key=lambda x: (0 if x["name"].lower() == q else 1 if x["name"].lower().startswith(q) else 2, len(x["name"])))
        return matches[:self.query_max_results]

    async def _get_elf_detail(self, t_id: str, name: str, avatar: str):
        cached = self.db.get_elf(t_id, self.cache_ttl_hours) if self.source_mode != "crawler_only" else None
        data = cached["data"] if cached else None
        screenshot = cached["screenshot_path"] if cached else None
        if self.source_mode == "cache_only" and not cached:
            return None, None
        if not data:
            try:
                data = await self.request.fetch_elf_data(t_id)
                self.db.save_elf(t_id, name, avatar, data, screenshot)
            except Exception as e:
                logger.error(f"抓取精灵数据失败 {t_id}: {e}")
                return None, None
        if self.reply_mode in ("image", "hybrid") and not screenshot:
            try:
                screenshot = str(await self.request.screenshot(t_id))
                self.db.save_elf(t_id, name, avatar, data, screenshot)
            except Exception as e:
                logger.error(f"生成精灵截图失败 {t_id}: {e}")
                screenshot = None
        return data, Path(screenshot) if screenshot else None

    async def _get_skill_detail(self, skill_id: str, name: str):
        cached = self.db.get_skill(skill_id, self.cache_ttl_hours) if self.source_mode != "crawler_only" else None
        data = cached["data"] if cached else None
        screenshot = cached["screenshot_path"] if cached else None
        if self.source_mode == "cache_only" and not cached:
            return None, None
        if not data:
            try:
                data = await self.request.fetch_skill_data(skill_id)
                self.db.save_skill(skill_id, name, data, screenshot)
            except Exception as e:
                logger.error(f"抓取技能数据失败 {skill_id}: {e}")
                return None, None
        if self.reply_mode in ("image", "hybrid") and not screenshot:
            try:
                screenshot = str(await self.request.skill_screenshot(skill_id))
                self.db.save_skill(skill_id, name, data, screenshot)
            except Exception as e:
                logger.error(f"生成技能截图失败 {skill_id}: {e}")
                screenshot = None
        return data, Path(screenshot) if screenshot else None

    async def _get_team_detail(self, team_id: str, name: str, description: str):
        cached = self.db.get_team(team_id, self.cache_ttl_hours) if self.source_mode != "crawler_only" else None
        data = cached["data"] if cached else None
        screenshot = cached["screenshot_path"] if cached else None
        if self.source_mode == "cache_only" and not cached:
            return None, None
        if not data:
            try:
                data = await self.request.fetch_team_data(team_id)
                self.db.save_team(team_id, name, description, data, screenshot)
            except Exception as e:
                logger.error(f"抓取配队数据失败 {team_id}: {e}")
                return None, None
        if self.reply_mode in ("image", "hybrid") and not screenshot:
            try:
                screenshot = str(await self.request.team_screenshot(team_id))
                self.db.save_team(team_id, name, description, data, screenshot)
            except Exception as e:
                logger.error(f"生成配队截图失败 {team_id}: {e}")
                screenshot = None
        return data, Path(screenshot) if screenshot else None

    async def _send_elf_result(self, event: AstrMessageEvent, target: Dict):
        t_id, name, avatar = target["t_id"], target["name"], target.get("avatar", "")
        data, screenshot = await self._get_elf_detail(t_id, name, avatar)
        if not data and self.reply_mode != "image":
            yield event.plain_result(f"❌ 获取精灵 {name} 数据失败。")
            return
        mode = self.reply_mode
        if mode == "image":
            if screenshot and screenshot.exists():
                yield event.image_result(str(screenshot))
            else:
                yield event.plain_result(f"❌ 无法生成 {name} 的图片。")
        elif mode == "text":
            if data:
                text = format_elf_text(data)
                await self._send_message(event, text)
            else:
                yield event.plain_result(f"❌ 无法获取 {name} 的文本数据。")
        elif mode == "hybrid":
            if data:
                text = format_elf_text(data)
                await self._send_message(event, text)
            if screenshot and screenshot.exists():
                yield event.image_result(str(screenshot))

    async def _send_skill_result(self, event: AstrMessageEvent, target: Dict):
        skill_id, name = target["skill_id"], target["name"]
        data, screenshot = await self._get_skill_detail(skill_id, name)
        if not data and self.reply_mode != "image":
            yield event.plain_result(f"❌ 获取技能 {name} 数据失败。")
            return
        mode = self.reply_mode
        if mode == "image":
            if screenshot and screenshot.exists():
                yield event.image_result(str(screenshot))
            else:
                yield event.plain_result(f"❌ 无法生成技能 {name} 的图片。")
        elif mode == "text":
            if data:
                text = format_skill_text(data)
                await self._send_message(event, text)
            else:
                yield event.plain_result(f"❌ 无法获取技能 {name} 的文本数据。")
        elif mode == "hybrid":
            if data:
                text = format_skill_text(data)
                await self._send_message(event, text)
            if screenshot and screenshot.exists():
                yield event.image_result(str(screenshot))

    async def _send_team_result(self, event: AstrMessageEvent, team_id: str, name: str, desc: str):
        data, screenshot = await self._get_team_detail(team_id, name, desc)
        if not data and self.reply_mode != "image":
            yield event.plain_result(f"❌ 获取配队 {team_id} 数据失败。")
            return
        mode = self.reply_mode
        if mode == "image":
            if screenshot and screenshot.exists():
                yield event.image_result(str(screenshot))
            else:
                yield event.plain_result(f"❌ 无法生成配队 {team_id} 的图片。")
        elif mode == "text":
            if data:
                text = format_team_text(data)
                await self._send_message(event, text)
            else:
                yield event.plain_result(f"❌ 无法获取配队 {team_id} 的文本数据。")
        elif mode == "hybrid":
            if data:
                text = format_team_text(data)
                await self._send_message(event, text)
            if screenshot and screenshot.exists():
                yield event.image_result(str(screenshot))

    @filter.command("查精灵")
    async def elf(self, event: AstrMessageEvent, name: str):
        matches = self._match_elf(name)
        if not matches:
            yield event.plain_result(f"❌ 未找到“{name}”。")
            return
        if len(matches) == 1:
            async for r in self._send_elf_result(event, matches[0]):
                if r: yield r
        else:
            key = self._selection_key(event)
            self._pending_set(key, name, matches, "elf")
            lines = [f"找到 {len(matches)} 个与“{name}”相关的精灵：", ""]
            for idx, m in enumerate(matches, 1):
                lines.append(f"{idx}. {m['name']}")
            lines.extend(["", "请回复序号查看详情，回复 0 取消。"])
            yield event.plain_result("\n".join(lines))

    @filter.command("查技能")
    async def skills(self, event: AstrMessageEvent, name: str):
        matches = self._match_skill(name)
        if not matches:
            yield event.plain_result(f"❌ 未找到技能“{name}”。")
            return
        if len(matches) == 1:
            async for r in self._send_skill_result(event, matches[0]):
                if r: yield r
        else:
            key = self._selection_key(event)
            self._pending_set(key, name, matches, "skill")
            lines = [f"找到 {len(matches)} 个与“{name}”相关的技能：", ""]
            for idx, m in enumerate(matches, 1):
                lines.append(f"{idx}. {m['name']}")
            lines.extend(["", "请回复序号查看详情，回复 0 取消。"])
            yield event.plain_result("\n".join(lines))

    @filter.command("查配队")
    async def search_team_by_pokemon(self, event: AstrMessageEvent, pokemon_name: str):
        yield event.plain_result(f" 正在查找包含“{pokemon_name}”的配队...")
        try:
            teams = await self.request.find_teams_by_pokemon(pokemon_name)
            if not teams:
                yield event.plain_result(f"❌ 未找到包含“{pokemon_name}”的配队。")
                return
            # 修改：无论多少个配队都列出选择列表，不再直接出图
            key = self._selection_key(event)
            self._pending_set(key, pokemon_name, teams, "team")
            lines = [f"找到 {len(teams)} 个包含“{pokemon_name}”的配队：", ""]
            for idx, t in enumerate(teams, 1):
                lines.append(f"{idx}. {t['name']} (ID: {t['team_id']})")
            lines.extend(["", "请回复序号查看配队详情，回复 0 取消。"])
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"查配队失败: {e}")
            yield event.plain_result(f"❌ 查询失败：{str(e)}")

    @filter.command("配队详情")
    async def team_detail(self, event: AstrMessageEvent, team_id: str):
        async for r in self._send_team_result(event, team_id, "", ""):
            if r: yield r

    @filter.command("抽精灵")
    async def lottery(self, event: AstrMessageEvent):
        if not self.elf_catalog:
            yield event.plain_result("❌ 精灵图鉴未加载。")
            return
        selected = random.sample(self.elf_catalog, 1)
        target = selected[0]
        yield event.plain_result(f" 正在抽取 {target['name']} ...")
        try:
            # 获取精灵详细数据（用于种族值和评价）
            data, _ = await self._get_elf_detail(target['t_id'], target['name'], target.get('avatar', ''))
            if not data:
                # 获取不到数据时仍然抽奖，但不显示种族值
                logger.warning(f"无法获取精灵 {target['name']} 的详细数据，抽奖图片将不含种族值")
            img = await self.request.single_lottery(target, elf_data=data)
            yield event.image_result(str(img))
        except Exception as e:
            logger.error(f"抽精灵失败: {e}")
            yield event.plain_result(f"❌ 抽精灵失败：{str(e)}")

    @filter.command("查蛋组")
    async def egg_group(self, event: AstrMessageEvent, name: str):
        yield event.plain_result(f"🥚 正在查询 {name} 的蛋组信息...")
        try:
            res = await self.request.get_egg_group(name)
            if not res["success"]:
                yield event.plain_result(f"❌ 查询失败：{res.get('error', '未知错误')}")
                return
            text = format_egg_group_text(res["name"], res["egg_group"], len(res.get("breedable_pokemons", [])), res.get("cannot_breed", False))
            await self._send_message(event, text)
            if self.reply_mode in ("image", "hybrid") and res.get("breedable_pokemons"):
                yield event.plain_result("️ 正在生成同蛋组精灵图片...")
                img = await self.request.generate_egg_group_image(res['name'], res['egg_group'], res['breedable_pokemons'], exclude_t_id=res.get('t_id', ''))
                if img:
                    yield event.image_result(str(img))
        except Exception as e:
            logger.error(f"蛋组异常: {e}")
            yield event.plain_result(f"❌ 查询失败：{str(e)}")

    @filter.command("查孵蛋")
    async def breeding_plan(self, event: AstrMessageEvent, known_with_gender: str, target_pokemon: str):
        kw = known_with_gender.strip()
        if kw.startswith(("公", "♂", "雄", "male")):
            gender = "male"
            known = kw[1:]
        elif kw.startswith(("母", "♀", "雌", "female")):
            gender = "female"
            known = kw[1:]
        else:
            yield event.plain_result("❌ 格式：查孵蛋 公精灵名 目标精灵名 或 查孵蛋 母精灵名 目标精灵名")
            return
        if not known or not target_pokemon:
            yield event.plain_result("❌ 请正确输入精灵名称。")
            return
        yield event.plain_result(f"🥚 正在规划从 {known}({'公' if gender=='male' else '母'}) 到 {target_pokemon} 的孵蛋路径...")
        try:
            result = await self.request.get_breeding_plan(known, target_pokemon, gender)
            if "error" in result:
                yield event.plain_result(f"❌ 规划失败：{result['error']}")
                return
            plan = result.get("breeding_plan")
            if not plan or plan.get("steps", 0) == 0:
                yield event.plain_result("❌ 无法找到可行的孵蛋路径")
                return
            mode = self.reply_mode
            if mode == "image":
                img = await self.request.breeding_plan_screenshot(known, target_pokemon, gender, result)
                if img:
                    yield event.image_result(str(img))
                else:
                    yield event.plain_result("❌ 生成孵蛋规划图片失败。")
            elif mode == "text":
                text = format_breeding_plan_text(result, plan, gender)
                await self._send_message(event, text)
            elif mode == "hybrid":
                text = format_breeding_plan_text(result, plan, gender)
                await self._send_message(event, text)
                img = await self.request.breeding_plan_screenshot(known, target_pokemon, gender, result)
                if img:
                    yield event.image_result(str(img))
        except Exception as e:
            logger.error(f"孵蛋规划异常: {e}")
            yield event.plain_result(f"❌ 查询失败：{str(e)}")

    @filter.command("查道具图鉴")
    async def props(self, event: AstrMessageEvent, name: str):
        yield event.plain_result("❌ 新站暂未提供道具图鉴。")

    @filter.command("查精灵蛋图鉴")
    async def elf_egg(self, event: AstrMessageEvent, name: str):
        yield event.plain_result("❌ 新站暂未提供精灵蛋图鉴。")

    # ========== 新增：查攻略 ==========
    @filter.command("查攻略")
    async def get_guide(self, event: AstrMessageEvent, name: str):
        """查攻略 精灵名 - 发送本地预存的攻略图片"""
        # 匹配精灵（复用现有匹配逻辑，保证名称标准化）
        matches = self._match_elf(name)
        if not matches:
            yield event.plain_result(f"❌ 未找到精灵“{name}”，无法获取攻略。")
            return

        # 取第一个最匹配的精灵名
        elf_name = matches[0]['name']

        # 定位本地攻略目录
        guide_dir = self.plugin_data_path / "guides" / elf_name
        if not guide_dir.exists() or not guide_dir.is_dir():
            yield event.plain_result(f"❌ 暂无“{elf_name}”的攻略图片。")
            return

        # 收集所有图片文件（按文件名排序）
        image_extensions = {'.png', '.jpg', '.jpeg', '.webp'}
        image_paths = sorted([
            p for p in guide_dir.iterdir()
            if p.suffix.lower() in image_extensions
        ])

        if not image_paths:
            yield event.plain_result(f"❌ 攻略文件夹“{elf_name}”中没有图片。")
            return

        # 发送图片（逐张发送）
        for img_path in image_paths:
            yield event.image_result(str(img_path))
            # 可添加短暂延迟防止刷屏（可选）
            # await asyncio.sleep(0.5)

        # 可选：发送完成提示
        yield event.plain_result(f"✅ 已发送 {elf_name} 的攻略图片共 {len(image_paths)} 张。")

    @filter.regex(r"^\s*\d+\s*$")
    async def select_index(self, event: AstrMessageEvent):
        key = self._selection_key(event)
        pending = self._pending_get(key)
        if not pending:
            return
        event.should_call_llm(True)
        event.stop_event()
        idx = int(event.get_message_str().strip())
        if idx == 0:
            self._pending_clear(key)
            yield event.plain_result("已取消选择。")
            return
        if idx < 1 or idx > len(pending.records):
            yield event.plain_result(f"请输入 1-{len(pending.records)} 或 0 取消。")
            return
        self._pending_clear(key)
        target = pending.records[idx-1]
        if pending.query_type == "elf":
            async for r in self._send_elf_result(event, target):
                if r: yield r
        elif pending.query_type == "skill":
            async for r in self._send_skill_result(event, target):
                if r: yield r
        elif pending.query_type == "team":
            async for r in self._send_team_result(event, target['team_id'], target.get('name', ''), target.get('description', '')):
                if r: yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("洛克重载")
    async def reload_plugin(self, event: AstrMessageEvent):
        try:
            self.elf_catalog = await self.request.fetch_catalog("https://wiki.lcx.cab/lk/index.php")
            self.skill_catalog = await self.request.fetch_skill_catalog("https://wiki.lcx.cab/lk/skill_list.php")
            yield event.plain_result(f"重载完成。精灵:{len(self.elf_catalog)} 技能:{len(self.skill_catalog)}")
        except Exception as e:
            yield event.plain_result(f"重载失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("洛克清缓存")
    async def clear_cache(self, event: AstrMessageEvent):
        cnt = len(self._pending)
        self._pending.clear()
        yield event.plain_result(f"已清除 {cnt} 个待选择会话。")

    async def terminate(self):
        for t in self._background_tasks:
            t.cancel()
        await self.request.close()