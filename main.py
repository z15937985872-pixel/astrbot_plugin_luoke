from pathlib import Path
import random
from datetime import datetime, timedelta

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .requests import Request

@register("roco_wiki", "Improved", "洛克王国 Wiki 图鉴助手（新站适配版）", "2.1.0")
class RocoWiki(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.plugin_data_path = Path(get_astrbot_data_path()) / "plugin_data" / "roco_wiki"
        self.plugin_data_path.mkdir(parents=True, exist_ok=True)

        self.request = Request(self.plugin_data_path)

        # 缓存数据结构: { "elf": (list_of_dict, expire_time), "skills": ... }
        self.cache = {}
        self.cache_ttl = timedelta(hours=24)

    async def initialize(self):
        """异步初始化，加载图鉴列表"""
        logger.info("正在初始化洛克王国 Wiki 图鉴（新站）...")
        await self._load_catalog("elf", "https://wiki.lcx.cab/lk/index.php")
        await self._load_catalog("skills", "https://wiki.lcx.cab/lk/skill_list.php")
        setattr(self, "props", [])
        setattr(self, "elf_egg", [])
        logger.info("洛克王国 Wiki 图鉴初始化完成（精灵、技能已加载，道具和精灵蛋暂不可用）")

    async def _load_catalog(self, key: str, url: str):
        if key in self.cache:
            items, expire = self.cache[key]
            if datetime.now() < expire:
                setattr(self, key, items)
                return
        try:
            if key == "elf":
                items = await self.request.fetch_catalog(url)
            elif key == "skills":
                items = await self.request.fetch_skill_catalog(url)
            else:
                items = []
                logger.warning(f"{key} 图鉴暂未实现抓取")
            setattr(self, key, items)
            self.cache[key] = (items, datetime.now() + self.cache_ttl)
            logger.info(f"加载 {key} 图鉴成功，共 {len(items)} 项")
        except Exception as e:
            logger.error(f"加载 {key} 图鉴失败: {e}")
            setattr(self, key, [])

    async def _query_elf(self, event: AstrMessageEvent, query_name: str):
        catalog = getattr(self, "elf", [])
        if not catalog:
            yield event.plain_result("❌ 精灵图鉴尚未加载成功，请稍后再试。")
            return
        matches = [item for item in catalog if query_name in item["name"]]
        if len(matches) == 1:
            target = matches[0]
        elif len(matches) > 1:
            hint = "、".join([m["name"] for m in matches[:5]])
            yield event.plain_result(f"⚠️ 找到多个匹配项：{hint}……\n请使用更精确的名称。")
            return
        else:
            yield event.plain_result(f"❌ 未找到“{query_name}”，请检查名称是否正确。")
            return
        try:
            yield event.plain_result(f"🔍 正在查询“{target['name']}”……")
            image_path = await self.request.screenshot(target["t_id"])
            yield event.image_result(str(image_path))
        except Exception as e:
            logger.error(f"截图失败: {e}")
            yield event.plain_result(f"❌ 查询失败：{str(e)}")

    @filter.command("查精灵图鉴")
    async def elf(self, event: AstrMessageEvent, name: str):
        async for result in self._query_elf(event, name):
            yield result

    @filter.command("查道具图鉴")
    async def props(self, event: AstrMessageEvent, name: str):
        yield event.plain_result("❌ 新站暂未提供道具图鉴，该命令暂不可用。")

    @filter.command("查技能图鉴")
    async def skills(self, event: AstrMessageEvent, name: str):
        catalog = getattr(self, "skills", [])
        if not catalog:
            yield event.plain_result("❌ 技能图鉴尚未加载成功，请稍后再试。")
            return
        matches = [item for item in catalog if name.lower() in item["name"].lower()]
        if len(matches) == 1:
            target = matches[0]
        elif len(matches) > 1:
            hint = "、".join([m["name"] for m in matches[:5]])
            yield event.plain_result(f"⚠️ 找到多个匹配项：{hint}……\n请使用更精确的名称。")
            return
        else:
            yield event.plain_result(f"❌ 未找到技能“{name}”，请检查名称是否正确。")
            return
        try:
            yield event.plain_result(f"🔍 正在查询技能“{target['name']}”……")
            image_path = await self.request.skill_screenshot(target["skill_id"])
            yield event.image_result(str(image_path))
        except Exception as e:
            logger.error(f"技能查询失败: {e}")
            yield event.plain_result(f"❌ 查询失败：{str(e)}")

    @filter.command("查精灵蛋图鉴")
    async def elf_egg(self, event: AstrMessageEvent, name: str):
        yield event.plain_result("❌ 新站暂未提供精灵蛋图鉴，该命令暂不可用。")

    @filter.command("抽精灵")
    async def lottery(self, event: AstrMessageEvent):
        elf_list = getattr(self, "elf", [])
        if len(elf_list) < 10:
            yield event.plain_result("❌ 精灵图鉴数量不足，无法抽奖。")
            return
        selected = random.sample(elf_list, 10)
        yield event.plain_result(f"🎲 正在抽取 {len(selected)} 只精灵，请稍候……")
        try:
            image_path = await self.request.lottery(selected)
            yield event.image_result(str(image_path))
        except Exception as e:
            logger.error(f"抽精灵失败: {e}")
            yield event.plain_result(f"❌ 抽精灵失败：{str(e)}")

    @filter.command("查配队")
    async def search_team_by_pokemon(self, event: AstrMessageEvent, pokemon_name: str):
        """查询包含指定精灵的所有配队"""
        yield event.plain_result(f"🔍 正在查找包含“{pokemon_name}”的配队，请稍候...")
        try:
            teams = await self.request.find_teams_by_pokemon(pokemon_name)
            if not teams:
                yield event.plain_result(f"❌ 未找到包含“{pokemon_name}”的配队。")
                return
            msg = f"📋 找到 {len(teams)} 个包含“{pokemon_name}”的配队：\n"
            for idx, team in enumerate(teams, 1):
                msg += f"{idx}. {team['name']} (ID: {team['team_id']})\n"
            msg += "\n使用 `配队详情 <队伍ID>` 查看详细配队图片。"
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"查配队失败: {e}")
            yield event.plain_result(f"❌ 查询失败：{str(e)}")

    @filter.command("配队详情")
    async def team_detail(self, event: AstrMessageEvent, team_id: str):
        """查看配队详情图片"""
        yield event.plain_result(f"🎯 正在生成配队 {team_id} 的详细图片...")
        try:
            image_path = await self.request.team_screenshot(team_id)
            yield event.image_result(str(image_path))
        except Exception as e:
            logger.error(f"生成配队截图失败: {e}")
            yield event.plain_result(f"❌ 生成失败：{str(e)}")

    async def terminate(self):
        await self.request.close()