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

    @filter.command("查精灵")
    async def elf(self, event: AstrMessageEvent, name: str):
        async for result in self._query_elf(event, name):
            yield result

    @filter.command("查道具图鉴")
    async def props(self, event: AstrMessageEvent, name: str):
        yield event.plain_result("❌ 新站暂未提供道具图鉴，该命令暂不可用。")

    @filter.command("查技能")
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

    @filter.command("查蛋组")
    async def egg_group(self, event: AstrMessageEvent, name: str):
        """查询精灵的蛋组信息，并返回同蛋组所有精灵的长图"""
        yield event.plain_result(f"🥚 正在查询 {name} 的蛋组信息...")
        try:
            result = await self.request.get_egg_group(name)
            if not result["success"]:
                yield event.plain_result(f"❌ 查询失败：{result.get('error', '未知错误')}")
                return
            
            # 文本回复
            msg = f"✨ 精灵：{result['name']}\n📦 蛋组：{result['egg_group']}"
            if result.get("cannot_breed"):
                msg += "\n⚠️ 该精灵无法繁殖"
            yield event.plain_result(msg)
            
            # 生成同蛋组精灵图片
            breedable = result.get("breedable_pokemons", [])
            if breedable:
                yield event.plain_result(f"🖼️ 正在生成同蛋组精灵图片（共 {len(breedable)} 只，排除自身后可能更少）...")
                image_path = await self.request.generate_egg_group_image(
                    result['name'], result['egg_group'], breedable, exclude_t_id=result.get('t_id', '')
                )
                if image_path:
                    yield event.image_result(str(image_path))
                else:
                    yield event.plain_result("📭 该蛋组没有其他精灵。")
            else:
                yield event.plain_result("📭 该蛋组没有其他精灵（或无法繁殖）。")
        except Exception as e:
            logger.error(f"蛋组查询异常: {e}")
            yield event.plain_result(f"❌ 查询失败：{str(e)}")

    @filter.command("查孵蛋")
    async def breeding_plan(self, event: AstrMessageEvent, known_with_gender: str, target_pokemon: str):
        """查询孵蛋路径：查孵蛋 公柴渣虫 幻影灵菇"""
        # 从第一个参数提取性别和精灵名称
        known_with_gender = known_with_gender.strip()
        if known_with_gender.startswith(("公", "♂", "雄", "male")):
            gender = "male"
            known_pokemon = known_with_gender[1:]  # 去掉第一个字符
        elif known_with_gender.startswith(("母", "♀", "雌", "female")):
            gender = "female"
            known_pokemon = known_with_gender[1:]
        else:
            yield event.plain_result("❌ 请使用格式：查孵蛋 公精灵名 目标精灵名 或 查孵蛋 母精灵名 目标精灵名")
            return
        
        if not known_pokemon or not target_pokemon:
            yield event.plain_result("❌ 请正确输入精灵名称，例如：查孵蛋 公柴渣虫 幻影灵菇")
            return
        
        gender_cn = "公" if gender == "male" else "母"
        yield event.plain_result(f"🥚 正在规划从 {known_pokemon}({gender_cn}) 到 {target_pokemon} 的孵蛋路径...")
        try:
            result = await self.request.get_breeding_plan(known_pokemon, target_pokemon, gender)
            
            if "error" in result:
                yield event.plain_result(f"❌ 规划失败：{result['error']}")
                return
            
            if result.get("error"):
                yield event.plain_result(f"❌ {result['error']}")
                return
            
            plan = result.get("breeding_plan")
            if not plan or plan.get("steps", 0) == 0:
                yield event.plain_result("❌ 无法找到可行的孵蛋路径")
                return
            
            # 构建回复文本
            msg_lines = []
            msg_lines.append(f"✨ 已知精灵：{result['parent_pokemon']['name']} ({'♂' if gender == 'male' else '♀'})")
            msg_lines.append(f"🎯 目标精灵：{result['target_pokemon']['name']}")
            msg_lines.append(f"📊 需要 {plan['steps']} 代{'（直接生蛋）' if plan['type'] == 'direct' else '（多代孵蛋）'}")
            msg_lines.append("")
            
            for step in plan["plan"]:
                p1 = step["parent1"]["name"]
                p2 = step["parent2"]["name"]
                p1_gender = "♂" if step["parent1_gender"] == "male" else "♀"
                p2_gender = "♂" if step["parent2_gender"] == "male" else "♀"
                result_name = step["result"]["name"]
                result_gender = "♂" if step["result_gender"] == "male" else "♀"
                note = step.get("note", "")
                
                step_desc = f"{step['step']}. {p1} ({p1_gender}) ❤️ {p2} ({p2_gender}) → {result_name} ({result_gender})"
                if note:
                    step_desc += f" （{note}）"
                msg_lines.append(step_desc)
            
            msg_lines.append("")
            msg_lines.append("⚠️ 注意：孵蛋并非100%成功，且子代性别随机。如需特定性别，可能需要多次尝试。")
            
            yield event.plain_result("\n".join(msg_lines))
        except Exception as e:
            logger.error(f"孵蛋规划异常: {e}")
            yield event.plain_result(f"❌ 查询失败：{str(e)}")

    async def terminate(self):
        await self.request.close()