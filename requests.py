import asyncio
import random
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from urllib.parse import quote

import aiohttp
from playwright.async_api import async_playwright, Playwright, Browser
from astrbot.api import logger

class Request:
    def __init__(self, data_path: Path):
        self.data_path = data_path
        self.screenshots_dir = data_path / "screenshots"
        self.lottery_dir = self.screenshots_dir / "lottery"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.lottery_dir.mkdir(parents=True, exist_ok=True)

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context = None
        self.elf_catalog_cache: List[Dict] = []
        
        # 配队详情缓存
        self.team_detail_cache: Dict[str, tuple] = {}
        self.team_cache_ttl = timedelta(hours=1)

    async def _ensure_browser(self):
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled"
                ]
            )
            self._context = await self._browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
                java_script_enabled=True,
                bypass_csp=True,
            )
            
            await self._context.route("**/*", self._handle_route)
            
            # 预热
            page = await self._context.new_page()
            await page.goto("https://wiki.lcx.cab/lk/index.php", wait_until="domcontentloaded", timeout=30000)
            await page.close()
    
    async def _handle_route(self, route):
        await route.continue_()
    
    async def fetch_catalog(self, url: str, retries: int = 3) -> List[Dict[str, str]]:
        """滚动加载所有精灵"""
        await self._ensure_browser()
        for attempt in range(retries):
            page = await self._context.new_page()
            try:
                await asyncio.sleep(random.uniform(1, 3))
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_selector(".pokemon-card", timeout=15000)
                
                previous_count = 0
                max_scrolls = 50
                scroll_attempts = 0
                while scroll_attempts < max_scrolls:
                    cards = await page.query_selector_all(".pokemon-card")
                    current_count = len(cards)
                    logger.info(f"当前已加载 {current_count} 个卡片")
                    
                    if current_count == previous_count:
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(1.5)
                        new_cards = await page.query_selector_all(".pokemon-card")
                        if len(new_cards) == current_count:
                            break
                        else:
                            previous_count = len(new_cards)
                            continue
                    
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(1.5)
                    previous_count = current_count
                    scroll_attempts += 1
                
                cards = await page.query_selector_all(".pokemon-card")
                logger.info(f"滚动加载完成，共找到 {len(cards)} 个卡片")
                
                if not cards:
                    debug_path = self.data_path / "debug_homepage.html"
                    content = await page.content()
                    with open(debug_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    logger.warning(f"未找到任何精灵卡片，页面 HTML 已保存至 {debug_path}")
                    return []
                
                result = []
                for card in cards:
                    t_id = None
                    onclick = await card.get_attribute("onclick")
                    if onclick:
                        match = re.search(r"navigateToDetail\('(\d+)'\)", onclick)
                        if match:
                            t_id = match.group(1)
                    if not t_id:
                        number_elem = await card.query_selector(".pokemon-number")
                        if number_elem:
                            number_text = await number_elem.inner_text()
                            match = re.search(r"(\d+)", number_text)
                            if match:
                                t_id = match.group(1)
                    if not t_id:
                        for attr in ["data-id", "data-pid", "data-index"]:
                            t_id = await card.get_attribute(attr)
                            if t_id:
                                break
                    if not t_id:
                        continue
                    
                    name_elem = await card.query_selector(".pokemon-name")
                    name = await name_elem.inner_text() if name_elem else ""
                    name = name.strip()
                    if not name:
                        title = await card.get_attribute("title")
                        if title:
                            name = title.strip()
                    if not name:
                        continue
                    
                    avatar = ""
                    img_elem = await card.query_selector("img")
                    if img_elem:
                        src = await img_elem.get_attribute("src")
                        if src:
                            if not src.startswith("http"):
                                src = "https://wiki.lcx.cab/lk/" + src
                            avatar = src
                    
                    result.append({"name": name, "t_id": t_id, "avatar": avatar})
                
                if result:
                    logger.info(f"成功抓取 {len(result)} 只精灵")
                    self.elf_catalog_cache = result
                    return result
                else:
                    logger.warning("找到卡片但未能提取任何名称或ID")
                    return []
                    
            except Exception as e:
                logger.error(f"抓取尝试 {attempt+1} 失败: {e}")
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(5)
            finally:
                await page.close()
        return []

    async def fetch_skill_catalog(self, url: str, retries: int = 3) -> List[Dict[str, str]]:
        await self._ensure_browser()
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        base_api_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path.replace("skill_list.php", "get_skill_data.php"), "", "", ""))
        
        all_skills = []
        page = 1
        has_more = True
        
        while has_more:
            params = {
                "page": page,
                "category": "all",
                "attribute": "all",
                "search": "",
                "sort": "",
                "direction": "desc",
                "energy_value": "all"
            }
            page_obj = None
            try:
                page_obj = await self._context.new_page()
                response = await page_obj.request.get(base_api_url, params=params)
                if not response.ok:
                    logger.error(f"请求技能 API 失败，状态码: {response.status}")
                    break
                data = await response.json()
                if not isinstance(data, list) or len(data) == 0:
                    has_more = False
                    break
                for skill in data:
                    skill_id = str(skill.get("id", ""))
                    name = skill.get("name", "").strip()
                    if skill_id and name:
                        all_skills.append({"name": name, "skill_id": skill_id})
                logger.info(f"抓取技能第 {page} 页，获得 {len(data)} 个技能")
                if len(data) < 20:
                    has_more = False
                else:
                    page += 1
            except Exception as e:
                logger.error(f"请求技能 API 失败 (第 {page} 页): {e}")
                if page == 1:
                    raise
                break
            finally:
                if page_obj:
                    await page_obj.close()
        if all_skills:
            logger.info(f"成功抓取 {len(all_skills)} 个技能")
            return all_skills
        else:
            logger.warning("未抓取到任何技能数据")
            return []

    # ========== 纯数据提取方法 ==========
    async def fetch_elf_data(self, t_id: str) -> dict:
        """仅提取精灵详情结构化数据，不生成截图"""
        await self._ensure_browser()
        detail_page = await self._context.new_page()
        try:
            url = f"https://wiki.lcx.cab/lk/detail.php?t_id={t_id}"
            logger.info(f"正在提取精灵数据: {url}")
            await detail_page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                await detail_page.wait_for_selector(".pokemon-title, .stats-bar-container", timeout=15000)
            except:
                pass
            await asyncio.sleep(1)

            data = await detail_page.evaluate('''() => {
                const getText = (sel) => {
                    const el = document.querySelector(sel);
                    return el ? el.innerText.trim() : '';
                };
                const getAttr = (sel, attr) => {
                    const el = document.querySelector(sel);
                    return el ? el.getAttribute(attr) || '' : '';
                };
                
                let fullTitle = getText('.pokemon-title');
                let name = fullTitle;
                let number = '';
                const match = fullTitle.match(/^(.*?)\\s+(No\\.\\d+)$/);
                if (match) {
                    name = match[1].trim();
                    number = match[2].trim();
                } else {
                    number = getText('.pokemon-number');
                }
                const attrImgs = Array.from(document.querySelectorAll('.attribute-icon')).map(img => img.src);
                const stats = {};
                document.querySelectorAll('.stat-item').forEach(item => {
                    const nameSpan = item.querySelector('.stat-name');
                    const valueSpan = item.querySelector('.stat-value');
                    if (nameSpan && valueSpan) {
                        stats[nameSpan.innerText.trim()] = valueSpan.innerText.trim();
                    }
                });
                let ability = getText('.abilities-text-display');
                if (!ability) ability = getText('.abilities-text');
                
                let avatar = '';
                const avatarSelectors = [
                    '.pokemon-image',
                    '.pokemon-image-container img',
                    '.detail-pokemon-image img',
                    '.pokemon-avatar img'
                ];
                for (const sel of avatarSelectors) {
                    const img = document.querySelector(sel);
                    if (img) {
                        avatar = img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '';
                        if (avatar) break;
                    }
                }
                if (!avatar) {
                    const allImgs = document.querySelectorAll('img');
                    for (const img of allImgs) {
                        const src = img.src || img.getAttribute('data-src');
                        if (src && (src.includes('pokemon') || src.includes('avatar') || src.includes('image'))) {
                            avatar = src;
                            break;
                        }
                    }
                }
                
                const evolution = [];
                const evoContainer = document.querySelector('.evolution-chain-container');
                if (evoContainer) {
                    const stages = evoContainer.querySelectorAll('.evolution-stage');
                    stages.forEach(stage => {
                        const stageName = stage.querySelector('.evolution-stage-title')?.innerText.trim() || '';
                        const cards = stage.querySelectorAll('.evolution-card');
                        cards.forEach(card => {
                            const link = card.querySelector('.pokemon-link');
                            const name = link ? link.innerText.trim() : '';
                            const tId = link ? link.getAttribute('href')?.match(/t_id=(\\d+)/)?.[1] : '';
                            const condition = card.querySelector('.evolution-condition small')?.innerText.trim() || '';
                            evolution.push({ name, t_id: tId, condition, stage: stageName });
                        });
                    });
                }
                
                const skills = { moves: [], xuemai: [], jinengshi: [] };
                document.querySelectorAll('#moves-container .skill-card-container').forEach(container => {
                    const simple = container.querySelector('.mode-simple');
                    if (simple) {
                        const name = simple.querySelector('.skill-name')?.innerText.trim() || '';
                        const typeIcon = simple.querySelector('.meta-row img:first-child')?.src || '';
                        const categoryIcon = simple.querySelector('.meta-row img:nth-child(2)')?.src || '';
                        const powerSpan = simple.querySelector('.power-badge');
                        const power = powerSpan ? powerSpan.innerText.trim() : '--';
                        const energySpan = simple.querySelector('.energy-chip');
                        const energy = energySpan ? energySpan.innerText.trim() : '0';
                        skills.moves.push({ name, typeIcon, categoryIcon, power, energy });
                    }
                });
                document.querySelectorAll('#xuemai-container .skill-card-container').forEach(container => {
                    const simple = container.querySelector('.mode-simple');
                    if (simple) {
                        const name = simple.querySelector('.skill-name')?.innerText.trim() || '';
                        const typeIcon = simple.querySelector('.meta-row img:first-child')?.src || '';
                        const categoryIcon = simple.querySelector('.meta-row img:nth-child(2)')?.src || '';
                        const powerSpan = simple.querySelector('.power-badge');
                        const power = powerSpan ? powerSpan.innerText.trim() : '--';
                        const energySpan = simple.querySelector('.energy-chip');
                        const energy = energySpan ? energySpan.innerText.trim() : '0';
                        skills.xuemai.push({ name, typeIcon, categoryIcon, power, energy });
                    }
                });
                document.querySelectorAll('#jinengshi-container .skill-card-container').forEach(container => {
                    const simple = container.querySelector('.mode-simple');
                    if (simple) {
                        const name = simple.querySelector('.skill-name')?.innerText.trim() || '';
                        const typeIcon = simple.querySelector('.meta-row img:first-child')?.src || '';
                        const categoryIcon = simple.querySelector('.meta-row img:nth-child(2)')?.src || '';
                        const powerSpan = simple.querySelector('.power-badge');
                        const power = powerSpan ? powerSpan.innerText.trim() : '--';
                        const energySpan = simple.querySelector('.energy-chip');
                        const energy = energySpan ? energySpan.innerText.trim() : '0';
                        skills.jinengshi.push({ name, typeIcon, categoryIcon, power, energy });
                    }
                });
                
                let typeChart = { attack: { '2x': [], '0.5x': [] }, defense: { '2x': [], '0.5x': [] } };
                if (typeof typeData !== 'undefined' && typeof resistData !== 'undefined' && typeof pokemonTypes !== 'undefined') {
                    const mainType = pokemonTypes[0] || '普通';
                    const subType = pokemonTypes[1] || null;
                    const allTypes = [...new Set([...Object.keys(typeData), ...Object.keys(resistData)])];
                    const attackEffect = {};
                    const defenseEffect = {};
                    allTypes.forEach(defender => {
                        let mul = 1;
                        if (typeData[mainType]?.includes(defender)) mul *= 2;
                        if (resistData[mainType]?.includes(defender)) mul *= 0.5;
                        if (subType) {
                            if (typeData[subType]?.includes(defender)) mul *= 2;
                            if (resistData[subType]?.includes(defender)) mul *= 0.5;
                        }
                        if (mul !== 1) attackEffect[defender] = mul;
                    });
                    allTypes.forEach(attacker => {
                        let mul = 1;
                        if (typeData[attacker]?.includes(mainType)) mul *= 2;
                        if (resistData[attacker]?.includes(mainType)) mul *= 0.5;
                        if (subType) {
                            if (typeData[attacker]?.includes(subType)) mul *= 2;
                            if (resistData[attacker]?.includes(subType)) mul *= 0.5;
                        }
                        if (mul !== 1) defenseEffect[attacker] = mul;
                    });
                    for (const [t, m] of Object.entries(attackEffect)) {
                        if (m === 2) typeChart.attack['2x'].push(t);
                        else if (m === 0.5) typeChart.attack['0.5x'].push(t);
                    }
                    for (const [t, m] of Object.entries(defenseEffect)) {
                        if (m === 2) typeChart.defense['2x'].push(t);
                        else if (m === 0.5) typeChart.defense['0.5x'].push(t);
                    }
                }
                
                return { name, number, attrImgs, stats, ability, avatar, evolution, skills, typeChart };
            }''')
            
            if data.get('avatar') and not data['avatar'].startswith('http'):
                data['avatar'] = 'https://wiki.lcx.cab/lk/' + data['avatar'].lstrip('/')
            
            if not data.get('stats'):
                data['stats'] = {"生命": "?", "物攻": "?", "魔攻": "?", "物防": "?", "魔防": "?", "速度": "?"}
                
            return data
        except Exception as e:
            logger.error(f"提取精灵数据失败 (t_id={t_id}): {e}")
            raise Exception(f"数据提取失败: {e}")
        finally:
            await detail_page.close()

    async def fetch_skill_data(self, skill_id: str) -> dict:
        """提取技能详情结构化数据"""
        await self._ensure_browser()
        detail_page = await self._context.new_page()
        try:
            url = f"https://wiki.lcx.cab/lk/skill_detail.php?id={skill_id}"
            await detail_page.goto(url, wait_until="networkidle", timeout=60000)
            await detail_page.wait_for_selector(".detailed-skill-card", timeout=15000)

            data = await detail_page.evaluate('''() => {
                const card = document.querySelector('.detailed-skill-card');
                if (!card) return {};
                const nameElem = card.querySelector('.detailed-skill-name');
                const name = nameElem ? nameElem.innerText.trim() : '';
                const iconElem = card.querySelector('.detailed-skill-icon');
                let icon = iconElem ? iconElem.src : '';
                const statCols = card.querySelectorAll('.detailed-skill-stats .stat-col');
                let energy = '', categoryIcon = '', categoryName = '', typeIcon = '', typeName = '', power = '';
                if (statCols.length >= 4) {
                    const energySpan = statCols[0].querySelector('.stat-value');
                    if (energySpan) energy = energySpan.innerText.trim();
                    let catText = statCols[1].innerText.trim();
                    if (catText) {
                        categoryName = catText;
                    } else {
                        const catImg = statCols[1].querySelector('.stat-icon');
                        if (catImg) {
                            categoryIcon = catImg.src;
                            const catMatch = categoryIcon.match(/\\/([^/]+)\\.webp/);
                            categoryName = catMatch ? catMatch[1] : '';
                        }
                    }
                    const catImgElem = statCols[1].querySelector('.stat-icon');
                    if (catImgElem) categoryIcon = catImgElem.src;
                    let typeText = statCols[2].innerText.trim();
                    if (typeText) {
                        typeName = typeText;
                    } else {
                        const typeImg = statCols[2].querySelector('.stat-icon');
                        if (typeImg) {
                            typeIcon = typeImg.src;
                            const typeMatch = typeIcon.match(/\\/([^/]+)\\.webp/);
                            typeName = typeMatch ? typeMatch[1] : '';
                        }
                    }
                    const typeImgElem = statCols[2].querySelector('.stat-icon');
                    if (typeImgElem) typeIcon = typeImgElem.src;
                    const powerSpan = statCols[3].querySelector('.stat-value');
                    if (powerSpan) power = powerSpan.innerText.trim();
                }
                const descElem = card.querySelector('.detailed-skill-desc');
                const description = descElem ? descElem.innerText.trim() : '';
                let acquireInfo = '';
                const sections = document.querySelectorAll('.section-card');
                for (const sec of sections) {
                    const title = sec.querySelector('h3, .section-title, h4');
                    if (title && (title.innerText.includes('获取') || title.innerText.includes('获得'))) {
                        const content = sec.querySelector('.alert, .card-body, .section-content');
                        if (content) {
                            acquireInfo = content.innerText.trim();
                            break;
                        }
                    }
                }
                if (!acquireInfo) {
                    const fallback = document.querySelector('.alert-success, .alert-info');
                    if (fallback) acquireInfo = fallback.innerText.trim();
                }
                const compatiblePokemons = [];
                const pokemonLinks = document.querySelectorAll('a[href*="detail.php?t_id="]');
                for (const link of pokemonLinks) {
                    const name = link.innerText.trim();
                    const href = link.getAttribute('href');
                    const match = href ? href.match(/t_id=(\\d+)/) : null;
                    const t_id = match ? match[1] : '';
                    let avatar = '';
                    const parent = link.closest('.pokemon-card, .skill-pokemon-item, .col-6');
                    if (parent) {
                        const img = parent.querySelector('img');
                        if (img) avatar = img.src;
                    }
                    if (name && t_id) {
                        compatiblePokemons.push({ name, t_id, avatar });
                    }
                }
                if (compatiblePokemons.length === 0) {
                    const items = document.querySelectorAll('.skill-learn-list .pokemon-name, .compatible-list a');
                    for (const item of items) {
                        const name = item.innerText.trim();
                        const href = item.getAttribute('href');
                        const match = href ? href.match(/t_id=(\\d+)/) : null;
                        const t_id = match ? match[1] : '';
                        if (name && t_id) {
                            compatiblePokemons.push({ name, t_id, avatar: '' });
                        }
                    }
                }
                return { name, icon, energy, categoryIcon, categoryName, typeIcon, typeName, power, description, acquireInfo, compatiblePokemons };
            }''')
            
            if data.get('icon') and not data['icon'].startswith('http'):
                data['icon'] = 'https://wiki.lcx.cab/lk/' + data['icon'].lstrip('/')
            if data.get('categoryIcon') and not data['categoryIcon'].startswith('http'):
                data['categoryIcon'] = 'https://wiki.lcx.cab/lk/' + data['categoryIcon'].lstrip('/')
            if data.get('typeIcon') and not data['typeIcon'].startswith('http'):
                data['typeIcon'] = 'https://wiki.lcx.cab/lk/' + data['typeIcon'].lstrip('/')
            
            if data.get('compatiblePokemons'):
                for pokemon in data['compatiblePokemons']:
                    if not pokemon.get('avatar') and self.elf_catalog_cache:
                        for elf in self.elf_catalog_cache:
                            if elf['t_id'] == pokemon['t_id']:
                                pokemon['avatar'] = elf.get('avatar', '')
                                break
                    if pokemon.get('avatar') and not pokemon['avatar'].startswith('http'):
                        pokemon['avatar'] = 'https://wiki.lcx.cab/lk/' + pokemon['avatar'].lstrip('/')
            return data
        except Exception as e:
            logger.error(f"提取技能数据失败 (skill_id={skill_id}): {e}")
            raise Exception(f"技能数据提取失败: {e}")
        finally:
            await detail_page.close()

    async def fetch_team_data(self, team_id: str) -> dict:
        """提取配队详情结构化数据"""
        if team_id in self.team_detail_cache:
            cached_data, expire_time = self.team_detail_cache[team_id]
            if datetime.now() < expire_time:
                return cached_data
        await self._ensure_browser()
        page = await self._context.new_page()
        try:
            url = f"https://wiki.lcx.cab/lk/team_builder.php?team_id={team_id}"
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(1)
            data = await page.evaluate('''() => {
                const result = {
                    team_name: '',
                    description: '',
                    trainer_skill: '',
                    pokemons: [],
                    type_analysis: { advantage: '', weakness: '' }
                };
                const nameInput = document.getElementById('teamName');
                if (nameInput) result.team_name = nameInput.value.trim();
                const descTextarea = document.getElementById('teamDescription');
                if (descTextarea) result.description = descTextarea.value.trim();
                const selectedSkillDiv = document.getElementById('selectedSkill');
                if (selectedSkillDiv) {
                    const skillName = selectedSkillDiv.querySelector('h3');
                    if (skillName) result.trainer_skill = skillName.innerText.trim();
                }
                const strongDiv = document.getElementById('teamStrongAgainst');
                const weakDiv = document.getElementById('teamWeakAgainst');
                if (strongDiv) result.type_analysis.advantage = strongDiv.innerText.trim();
                if (weakDiv) result.type_analysis.weakness = weakDiv.innerText.trim();
                const filledSlots = document.querySelectorAll('.pokemon-slot.filled');
                filledSlots.forEach(slot => {
                    const pokemon = { name: '', t_id: '', avatar: '', pvp_stats: {}, base_stats: {}, skills: [] };
                    const nameHeader = slot.querySelector('.card-header h5');
                    if (nameHeader) pokemon.name = nameHeader.innerText.trim();
                    const img = slot.querySelector('.card-body img');
                    if (img && img.src) pokemon.avatar = img.src;
                    const statsContainer = slot.querySelector('.stats-bar-chart-container');
                    if (statsContainer && statsContainer.dataset) {
                        const baseMap = {
                            '生命': statsContainer.dataset.hp,
                            '物攻': statsContainer.dataset.attack,
                            '物防': statsContainer.dataset.defense,
                            '魔攻': statsContainer.dataset.specialAttack,
                            '魔防': statsContainer.dataset.specialDefense,
                            '速度': statsContainer.dataset.speed
                        };
                        for (const [k, v] of Object.entries(baseMap)) {
                            if (v) pokemon.base_stats[k] = v;
                        }
                        if (statsContainer.dataset.pvpLife) pokemon.pvp_stats['生命'] = statsContainer.dataset.pvpLife;
                        if (statsContainer.dataset.pvpPatk) pokemon.pvp_stats['物攻'] = statsContainer.dataset.pvpPatk;
                        if (statsContainer.dataset.pvpPdef) pokemon.pvp_stats['物防'] = statsContainer.dataset.pvpPdef;
                        if (statsContainer.dataset.pvpMatk) pokemon.pvp_stats['魔攻'] = statsContainer.dataset.pvpMatk;
                        if (statsContainer.dataset.pvpMdef) pokemon.pvp_stats['魔防'] = statsContainer.dataset.pvpMdef;
                        if (statsContainer.dataset.pvpSpd) pokemon.pvp_stats['速度'] = statsContainer.dataset.pvpSpd;
                    }
                    const moveSlots = slot.querySelectorAll('.move-slot.selected');
                    moveSlots.forEach(moveSlot => {
                        const skillNameSpan = moveSlot.querySelector('.skill-name-small');
                        if (skillNameSpan) {
                            let skillName = skillNameSpan.innerText.trim();
                            skillName = skillName.replace(/自带|血脉|技能石/g, '').trim();
                            if (skillName) pokemon.skills.push(skillName);
                        }
                    });
                    const tId = slot.dataset.pokemonId;
                    if (tId) pokemon.t_id = tId;
                    if (pokemon.name) result.pokemons.push(pokemon);
                });
                return result;
            }''')
            if self.elf_catalog_cache:
                for p in data['pokemons']:
                    if not p.get('avatar') and p.get('t_id'):
                        for elf in self.elf_catalog_cache:
                            if elf['t_id'] == p['t_id']:
                                p['avatar'] = elf.get('avatar', '')
                                break
                    if p.get('avatar') and not p['avatar'].startswith('http'):
                        p['avatar'] = 'https://wiki.lcx.cab/lk/' + p['avatar'].lstrip('/')
            self.team_detail_cache[team_id] = (data, datetime.now() + self.team_cache_ttl)
            return data
        except Exception as e:
            logger.error(f"获取配队数据失败 team_id={team_id}: {e}")
            raise Exception(f"配队数据提取失败: {e}")
        finally:
            await page.close()

    # ========== 截图方法 ==========
    async def screenshot(self, t_id: str) -> Path:
        """生成精灵详情截图，优先使用缓存"""
        cache_path = self.screenshots_dir / f"{t_id}.png"
        if cache_path.exists():
            return cache_path
        data = await self.fetch_elf_data(t_id)
        return await self._generate_elf_screenshot(t_id, data, cache_path)

    async def skill_screenshot(self, skill_id: str) -> Path:
        cache_path = self.screenshots_dir / f"skill_{skill_id}.png"
        if cache_path.exists():
            return cache_path
        data = await self.fetch_skill_data(skill_id)
        return await self._generate_skill_screenshot(skill_id, data, cache_path)

    async def team_screenshot(self, team_id: str) -> Path:
        cache_path = self.screenshots_dir / f"team_{team_id}.png"
        if cache_path.exists():
            return cache_path
        data = await self.fetch_team_data(team_id)
        return await self._generate_team_screenshot(team_id, data, cache_path)

    # ========== 抽奖方法（修改版） ==========
    async def single_lottery(self, item: Dict[str, str], elf_data: Optional[dict] = None) -> Path:
        """单张精灵大图抽奖，支持显示种族值及评价"""
        await self._ensure_browser()
        t_id = item["t_id"]
        name = item["name"]
        avatar = item.get("avatar", "")
        
        if not avatar:
            page_temp = await self._context.new_page()
            try:
                await page_temp.goto("https://wiki.lcx.cab/lk/index.php", wait_until="domcontentloaded", timeout=30000)
                card = await page_temp.query_selector(f".pokemon-card[onclick*='navigateToDetail(\"{t_id}\")']")
                if card:
                    img_elem = await card.query_selector("img")
                    if img_elem:
                        src = await img_elem.get_attribute("src")
                        if src:
                            if not src.startswith("http"):
                                src = "https://wiki.lcx.cab/lk/" + src
                            avatar = src
            except:
                pass
            finally:
                await page_temp.close()
        
        if not avatar:
            avatar = "https://via.placeholder.com/512?text=No+Image"
        
        # 计算种族值总和和评价
        stats = {}
        total_stat = 0
        if elf_data:
            stats = elf_data.get('stats', {})
            for v in stats.values():
                try:
                    total_stat += int(v)
                except (ValueError, TypeError):
                    pass
        
        # 评价语
        if total_stat >= 700:
            comment = "🌈 运气爆棚！这只精灵潜力无限！"
        elif total_stat >= 600:
            comment = "👍 今天运气不错哦！"
        elif total_stat >= 500:
            comment = "🍀 还算可以，继续加油！"
        else:
            comment = "💪 别灰心，明天会更好！"
        
        # 构建种族值显示行
        stat_names = ["生命", "物攻", "魔攻", "物防", "魔防", "速度"]
        stats_lines = []
        for sn in stat_names:
            val = stats.get(sn, '?')
            stats_lines.append(f"{sn}: {val}")
        stats_html = "　".join(stats_lines)  # 全角空格分隔
        total_html = f"种族值总和: {total_stat}"
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.lottery_dir / f"single_lottery_{timestamp}.png"
        
        html_page = await self._context.new_page()
        try:
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <style>
                    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                    body {{
                        width: 1024px;
                        height: 1024px;
                        background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
                    }}
                    .card {{
                        width: 100%;
                        height: 100%;
                        background: radial-gradient(circle at 30% 20%, rgba(255,255,240,0.2), rgba(0,0,0,0.3));
                        display: flex;
                        flex-direction: column;
                        justify-content: center;
                        align-items: center;
                        text-align: center;
                        padding: 40px;
                    }}
                    .avatar {{
                        width: 70%;
                        max-width: 450px;
                        background: rgba(255,255,255,0.2);
                        border-radius: 50%;
                        padding: 20px;
                        box-shadow: 0 20px 40px rgba(0,0,0,0.4);
                        backdrop-filter: blur(5px);
                    }}
                    .avatar img {{
                        width: 100%;
                        height: auto;
                        object-fit: contain;
                        border-radius: 50%;
                        background: white;
                    }}
                    .name {{
                        margin-top: 30px;
                        font-size: 44px;
                        font-weight: bold;
                        color: #ffdd88;
                        text-shadow: 4px 4px 8px rgba(0,0,0,0.5);
                        background: rgba(0,0,0,0.5);
                        padding: 10px 30px;
                        border-radius: 60px;
                        display: inline-block;
                    }}
                    .stats {{
                        margin-top: 20px;
                        background: rgba(0,0,0,0.6);
                        padding: 12px 24px;
                        border-radius: 40px;
                        color: white;
                        font-size: 18px;
                        font-weight: bold;
                        backdrop-filter: blur(4px);
                    }}
                    .comment {{
                        margin-top: 20px;
                        font-size: 26px;
                        color: #FFE484;
                        text-shadow: 2px 2px 4px black;
                        background: rgba(0,0,0,0.4);
                        padding: 8px 20px;
                        border-radius: 40px;
                    }}
                    .footer {{
                        margin-top: 30px;
                        font-size: 20px;
                        color: rgba(255,255,255,0.8);
                        background: rgba(0,0,0,0.4);
                        padding: 6px 20px;
                        border-radius: 40px;
                    }}
                </style>
            </head>
            <body>
                <div class="card">
                    <div class="avatar">
                        <img src="{avatar}" referrerpolicy="no-referrer" 
                             onerror="this.onerror=null;this.src='https://via.placeholder.com/512?text=LoadFailed'">
                    </div>
                    <div class="name">{name}</div>
                    <div class="stats">{stats_html}<br>{total_html}</div>
                    <div class="comment">{comment}</div>
                    <div class="footer">✨ 洛克王国 Wiki 今日运势 ✨</div>
                </div>
            </body>
            </html>
            """
            await html_page.set_content(html_content, wait_until="commit", timeout=20000)
            await html_page.wait_for_timeout(1000)
            await html_page.set_viewport_size({"width": 1024, "height": 1024})
            await html_page.screenshot(path=str(out_path), full_page=True)
            return out_path
        except Exception as e:
            raise Exception(f"单张抽奖截图生成失败: {e}")
        finally:
            await html_page.close()

    async def lottery(self, items: List[Dict[str, str]]) -> Path:
        """十连抽（实际返回最后一个，保留原行为）"""
        tasks = [self.single_lottery(item) for item in items]
        results = await asyncio.gather(*tasks)
        return results[-1] if results else None

    # ========== 新增：孵蛋规划截图 ==========
    async def breeding_plan_screenshot(self, parent_name: str, target_name: str, gender: str, plan_data: dict) -> Optional[Path]:
        """生成孵蛋规划截图"""
        await self._ensure_browser()
        cache_path = self.screenshots_dir / f"breeding_{parent_name}_{target_name}_{gender}.png"
        if cache_path.exists():
            return cache_path

        parent = plan_data.get('parent_pokemon', {})
        target = plan_data.get('target_pokemon', {})
        plan = plan_data.get('breeding_plan', {})
        steps = plan.get('plan', [])

        # 构建 HTML 步骤列表
        steps_html = ""
        for step in steps:
            p1 = step['parent1']['name']
            p2 = step['parent2']['name']
            p1_gender = '♂' if step['parent1_gender'] == 'male' else '♀'
            p2_gender = '♂' if step['parent2_gender'] == 'male' else '♀'
            res = step['result']['name']
            res_gender = '♂' if step['result_gender'] == 'male' else '♀'
            note = step.get('note', '')
            step_line = f"{step['step']}. {p1}({p1_gender}) + {p2}({p2_gender}) → {res}({res_gender})"
            if note:
                step_line += f" （{note}）"
            steps_html += f"<div class='step-item'>{step_line}</div>"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>洛克王国孵蛋规划</title>
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{
                    background: #1a2a3a;
                    padding: 20px;
                    font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
                }}
                .container {{
                    max-width: 700px;
                    margin: 0 auto;
                    background: white;
                    border-radius: 32px;
                    overflow: hidden;
                    box-shadow: 0 20px 40px rgba(0,0,0,0.3);
                }}
                .header {{
                    background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
                    padding: 20px;
                    color: white;
                }}
                .header h1 {{
                    font-size: 24px;
                    margin-bottom: 8px;
                }}
                .parent-target {{
                    display: flex;
                    justify-content: space-between;
                    background: #fff0f5;
                    padding: 16px;
                    margin: 20px;
                    border-radius: 24px;
                }}
                .info-card {{
                    text-align: center;
                    flex: 1;
                }}
                .info-card .label {{
                    font-size: 12px;
                    color: #c94f6f;
                }}
                .info-card .name {{
                    font-size: 20px;
                    font-weight: bold;
                    margin-top: 6px;
                }}
                .info-card .gender {{
                    font-size: 16px;
                }}
                .steps-section {{
                    margin: 20px;
                }}
                .steps-title {{
                    font-size: 18px;
                    font-weight: bold;
                    margin-bottom: 12px;
                    border-left: 4px solid #f5576c;
                    padding-left: 12px;
                }}
                .step-item {{
                    background: #f8f9fc;
                    margin: 8px 0;
                    padding: 12px;
                    border-radius: 16px;
                    font-size: 14px;
                    border: 1px solid #ffe0e7;
                }}
                .footer {{
                    background: #f0f2f5;
                    text-align: center;
                    padding: 12px;
                    font-size: 12px;
                    color: #6c757d;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>🥚 孵蛋路径规划</h1>
                    <div>从已知精灵到目标精灵</div>
                </div>
                <div class="parent-target">
                    <div class="info-card">
                        <div class="label">已知精灵</div>
                        <div class="name">{parent.get('name', '?')}</div>
                        <div class="gender">{'♂ 公' if gender == 'male' else '♀ 母'}</div>
                    </div>
                    <div class="info-card">
                        <div class="label">🎯 目标精灵</div>
                        <div class="name">{target.get('name', '?')}</div>
                    </div>
                </div>
                <div class="steps-section">
                    <div class="steps-title">📋 繁殖路线（共 {plan.get('steps', 0)} 代）</div>
                    {steps_html if steps_html else '<div>无法生成具体步骤</div>'}
                </div>
                <div class="footer">
                    ⚠️ 孵蛋并非100%成功，子代性别随机 | 数据来源于洛克Wiki
                </div>
            </div>
        </body>
        </html>
        """

        page = await self._context.new_page()
        try:
            await page.set_content(html_content, wait_until="commit", timeout=20000)
            await page.wait_for_timeout(500)
            await page.set_viewport_size({"width": 700, "height": 1})
            await page.screenshot(path=str(cache_path), full_page=True)
            return cache_path
        except Exception as e:
            logger.error(f"生成孵蛋规划截图失败: {e}")
            return None
        finally:
            await page.close()

    # ========== 以下为原有方法（保持不变） ==========
    async def fetch_all_teams(self, url: str = "https://wiki.lcx.cab/lk/recommended_teams.php") -> List[Dict]:
        await self._ensure_browser()
        page = await self._context.new_page()
        try:
            logger.info(f"正在抓取配队列表页: {url}")
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_selector(".team-card", timeout=15000)
            await asyncio.sleep(1)
            team_cards = await page.query_selector_all(".team-card")
            logger.info(f"找到 {len(team_cards)} 个队伍卡片")
            teams = []
            for card in team_cards:
                team_id = None
                detail_btn = await card.query_selector(".btn-detail")
                if detail_btn:
                    onclick = await detail_btn.get_attribute("onclick")
                    if onclick:
                        match = re.search(r"viewTeam\((\d+)\)", onclick)
                        if match:
                            team_id = match.group(1)
                if not team_id:
                    name_elem = await card.query_selector(".team-name")
                    if name_elem:
                        name_text = await name_elem.inner_text()
                        match = re.search(r"编号:(\d+)", name_text)
                        if match:
                            team_id = match.group(1)
                if not team_id:
                    logger.warning("无法提取队伍 ID，跳过该卡片")
                    continue
                name_elem = await card.query_selector(".team-name")
                team_name = await name_elem.inner_text() if name_elem else f"队伍 {team_id}"
                team_name = team_name.strip()
                desc_elem = await card.query_selector(".team-description-bottom p")
                if not desc_elem:
                    desc_elem = await card.query_selector(".card-body .text-muted")
                description = await desc_elem.inner_text() if desc_elem else ""
                description = description.strip()
                teams.append({
                    "team_id": team_id,
                    "name": team_name,
                    "description": description
                })
            logger.info(f"成功抓取到 {len(teams)} 个配队")
            if not teams:
                content = await page.content()
                debug_path = self.data_path / "debug_team_list.html"
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(content)
                logger.warning(f"未找到任何配队，页面已保存至 {debug_path}")
            return teams
        except Exception as e:
            logger.error(f"抓取配队列表失败: {e}")
            return []
        finally:
            await page.close()

    async def find_teams_by_pokemon(self, pokemon_name_or_id: str, max_concurrent: int = 5) -> List[Dict]:
        all_teams = await self.fetch_all_teams()
        if not all_teams:
            return []
        semaphore = asyncio.Semaphore(max_concurrent)
        async def check_team(team: Dict) -> Optional[Dict]:
            async with semaphore:
                try:
                    detail = await self.fetch_team_data(team['team_id'])
                    names = [p['name'] for p in detail.get('pokemons', [])]
                    ids = [p['t_id'] for p in detail.get('pokemons', []) if p.get('t_id')]
                    if pokemon_name_or_id in names or pokemon_name_or_id in ids:
                        return {
                            "team_id": team['team_id'],
                            "name": team['name'],
                            "description": team.get('description', '')
                        }
                except Exception as e:
                    logger.warning(f"检查配队 {team['team_id']} 时出错: {e}")
                return None
        tasks = [check_team(team) for team in all_teams]
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]

    # ========== 蛋组相关 ==========
    async def get_egg_group(self, elf_name: str) -> dict:
        encoded_name = quote(elf_name)
        url = f"https://wiki.lcx.cab/lk/egg_group.php?action=search&name={encoded_name}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {"success": False, "error": f"HTTP {resp.status}"}
                data = await resp.json()
        searched = data.get("searched_pokemon")
        if not searched:
            return {"success": False, "error": "未找到该精灵"}
        return {
            "success": True,
            "name": searched.get("name", elf_name),
            "t_id": searched.get("t_id", ""),
            "egg_group": searched.get("egg_group", ""),
            "cannot_breed": data.get("cannot_breed", False),
            "breedable_pokemons": data.get("breedable_pokemons", [])
        }

    async def generate_egg_group_image(self, elf_name: str, egg_group: str, breedable_list: List[Dict], exclude_t_id: str = "") -> Optional[Path]:
        filtered = [p for p in breedable_list if p.get("t_id") != exclude_t_id]
        if not filtered:
            return None
        cache_path = self.screenshots_dir / f"egg_group_{elf_name}.png"
        if cache_path.exists():
            return cache_path
        items_html = ""
        for pokemon in filtered:
            t_id = pokemon.get("t_id", "")
            name = pokemon.get("name", "")
            attrs = pokemon.get("attributes", "")
            avatar = ""
            if self.elf_catalog_cache:
                for elf in self.elf_catalog_cache:
                    if elf["t_id"] == t_id:
                        avatar = elf.get("avatar", "")
                        break
            if not avatar:
                avatar = "https://via.placeholder.com/80?text=No+Img"
            items_html += f"""
            <div class="pokemon-item">
                <img class="avatar" src="{avatar}" referrerpolicy="no-referrer" onerror="this.src='https://via.placeholder.com/80?text=Error'">
                <div class="name">{name}</div>
                <div class="attributes">{attrs}</div>
            </div>
            """
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{
                    background: #1a2a3a;
                    padding: 20px;
                    font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
                }}
                .container {{
                    max-width: 800px;
                    margin: 0 auto;
                    background: white;
                    border-radius: 24px;
                    overflow: hidden;
                    box-shadow: 0 20px 40px rgba(0,0,0,0.3);
                }}
                .header {{
                    background: linear-gradient(135deg, #f5af19, #f12711);
                    padding: 20px;
                    color: white;
                }}
                .header h1 {{
                    font-size: 24px;
                    margin-bottom: 8px;
                }}
                .header p {{
                    font-size: 14px;
                    opacity: 0.9;
                }}
                .grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
                    gap: 16px;
                    padding: 20px;
                }}
                .pokemon-item {{
                    background: #f8f9fc;
                    border-radius: 16px;
                    padding: 12px;
                    text-align: center;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.05);
                }}
                .avatar {{
                    width: 80px;
                    height: 80px;
                    object-fit: contain;
                    margin-bottom: 8px;
                }}
                .name {{
                    font-weight: bold;
                    font-size: 14px;
                    margin-bottom: 4px;
                }}
                .attributes {{
                    font-size: 12px;
                    color: #6c757d;
                }}
                .footer {{
                    background: #f0f2f5;
                    text-align: center;
                    padding: 12px;
                    font-size: 12px;
                    color: #6c757d;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>🥚 {elf_name} 的同蛋组精灵</h1>
                    <p>蛋组：{egg_group} | 共 {len(filtered)} 只（已排除自身）</p>
                </div>
                <div class="grid">
                    {items_html}
                </div>
                <div class="footer">洛克王国 Wiki 数据 | 蛋组信息</div>
            </div>
        </body>
        </html>
        """
        await self._ensure_browser()
        page = await self._context.new_page()
        try:
            await page.set_content(html_content, wait_until="commit", timeout=20000)
            await page.wait_for_timeout(1000)
            await page.set_viewport_size({"width": 800, "height": 1})
            await page.screenshot(path=str(cache_path), full_page=True)
            return cache_path
        finally:
            await page.close()

    # ========== 孵蛋规划 API ==========
    async def get_breeding_plan(self, parent_name: str, target_name: str, gender: str, shiny_only: bool = False) -> dict:
        encoded_parent = quote(parent_name)
        encoded_target = quote(target_name)
        url = f"https://wiki.lcx.cab/lk/egg_group_new.php?action=search&parent={encoded_parent}&target={encoded_target}&gender={gender}"
        if shiny_only:
            url += "&shiny_only=1"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {"error": f"HTTP {resp.status}"}
                try:
                    data = await resp.json()
                    return data
                except Exception as e:
                    logger.error(f"解析生蛋规划 JSON 失败: {e}")
                    return {"error": "服务器返回了无效数据"}

    # ========== 内部生成截图方法（原有） ==========
    async def _generate_elf_screenshot(self, t_id: str, data: dict, cache_path: Path) -> Path:
        """内部方法：根据已提取的数据生成精灵截图"""
        await self._ensure_browser()
        
        avatar_src = data.get('avatar') or 'https://via.placeholder.com/150?text=No+Image'
        
        stats_html = ""
        stat_names = ["生命", "物攻", "魔攻", "物防", "魔防", "速度"]
        for stat_name in stat_names:
            value = data['stats'].get(stat_name, "?")
            try:
                val_int = int(value)
                width_percent = min(100, (val_int / 200) * 100) if val_int else 0
            except:
                width_percent = 0
            stats_html += f"""
            <div class="stat-row">
                <span class="stat-name">{stat_name}</span>
                <div class="stat-bar-bg"><div class="stat-bar" style="width: {width_percent}%;"></div></div>
                <span class="stat-value">{value}</span>
            </div>
            """
        
        attr_html = ""
        for attr_url in data.get('attrImgs', []):
            attr_html += f'<img src="{attr_url}" class="attr-icon">'
        if not attr_html:
            attr_html = '<span class="text-muted">无属性</span>'
        
        evolution_html = '<div class="evolution-section">'
        if data.get('evolution') and len(data['evolution']) > 0:
            evolution_html += '<div class="section-title"><i class="fas fa-link"></i> 进化链</div><div class="evolution-list">'
            for evo in data['evolution']:
                evolution_html += f'<div class="evolution-item">{evo["stage"]}: {evo["name"]} ({evo["condition"]})</div>'
            evolution_html += '</div>'
        else:
            evolution_html += '<div class="section-title"><i class="fas fa-link"></i> 进化链</div><div>暂无进化信息</div>'
        evolution_html += '</div>'
        
        def build_skill_section(title, skills_list):
            if not skills_list:
                return ''
            html = f'<div class="skills-subsection"><div class="section-title">{title}</div><div class="skills-grid">'
            for sk in skills_list[:20]:
                html += f'''
                <div class="skill-item">
                    <div class="skill-name">{sk["name"]}</div>
                    <div class="skill-meta">
                        <img src="{sk["typeIcon"]}" class="skill-type-icon"> 
                        <img src="{sk["categoryIcon"]}" class="skill-cat-icon"> 
                        威力:{sk["power"]} 耗能:{sk["energy"]}
                    </div>
                </div>
                '''
            html += '</div></div>'
            return html
        
        skills = data.get('skills', {'moves': [], 'xuemai': [], 'jinengshi': []})
        skills_html = '<div class="skills-section">'
        skills_html += build_skill_section('精灵技能', skills.get('moves', []))
        skills_html += build_skill_section('血脉技能', skills.get('xuemai', []))
        skills_html += build_skill_section('技能石', skills.get('jinengshi', []))
        skills_html += '</div>'
        
        type_chart = data.get('typeChart', {'attack': {'2x': [], '0.5x': []}, 'defense': {'2x': [], '0.5x': []}})
        type_chart_html = '<div class="typechart-section"><div class="section-title">属性克制</div><div class="typechart-grid">'
        type_chart_html += '<div class="typechart-col"><div class="subtitle">作为攻击方时</div>'
        if len(type_chart['attack']['2x']) > 0:
            type_chart_html += f'<div class="damage-item"><span class="badge-2x">2倍</span> {", ".join(type_chart["attack"]["2x"])}</div>'
        if len(type_chart['attack']['0.5x']) > 0:
            type_chart_html += f'<div class="damage-item"><span class="badge-half">1/2倍</span> {", ".join(type_chart["attack"]["0.5x"])}</div>'
        type_chart_html += '</div>'
        type_chart_html += '<div class="typechart-col"><div class="subtitle">作为防守方时</div>'
        if len(type_chart['defense']['2x']) > 0:
            type_chart_html += f'<div class="damage-item"><span class="badge-2x">2倍</span> {", ".join(type_chart["defense"]["2x"])}</div>'
        if len(type_chart['defense']['0.5x']) > 0:
            type_chart_html += f'<div class="damage-item"><span class="badge-half">1/2倍</span> {", ".join(type_chart["defense"]["0.5x"])}</div>'
        type_chart_html += '</div></div></div>'
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>{data['name']}</title>
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{
                    background: #1a2a3a;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    padding: 20px;
                    font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
                }}
                .card {{
                    width: 800px;
                    background: white;
                    border-radius: 24px;
                    box-shadow: 0 20px 40px rgba(0,0,0,0.3);
                    overflow: hidden;
                }}
                .card-header {{
                    background: linear-gradient(135deg, #f5af19, #f12711);
                    padding: 20px;
                    display: flex;
                    justify-content: space-between;
                    align-items: baseline;
                    color: white;
                }}
                .pokemon-name {{
                    font-size: 32px;
                    font-weight: 800;
                }}
                .pokemon-number {{
                    font-size: 18px;
                    background: rgba(0,0,0,0.3);
                    padding: 4px 12px;
                    border-radius: 40px;
                }}
                .avatar-area {{
                    display: flex;
                    justify-content: center;
                    padding: 20px;
                    background: #fef7e0;
                }}
                .avatar {{
                    width: 150px;
                    height: 150px;
                    object-fit: contain;
                    background: white;
                    border-radius: 50%;
                    padding: 10px;
                    box-shadow: 0 8px 20px rgba(0,0,0,0.2);
                }}
                .attr-area {{
                    text-align: center;
                    padding: 10px;
                }}
                .attr-icon {{
                    width: 32px;
                    height: 32px;
                    margin: 0 5px;
                }}
                .stats-container {{
                    padding: 20px;
                    background: #f8f9fc;
                }}
                .stat-row {{
                    display: flex;
                    align-items: center;
                    margin-bottom: 12px;
                    gap: 12px;
                }}
                .stat-name {{
                    width: 50px;
                    font-weight: 600;
                }}
                .stat-bar-bg {{
                    flex: 1;
                    height: 10px;
                    background: #e0e4e8;
                    border-radius: 10px;
                    overflow: hidden;
                }}
                .stat-bar {{
                    height: 100%;
                    background: #3b9eff;
                    border-radius: 10px;
                }}
                .stat-value {{
                    width: 40px;
                    text-align: right;
                    font-weight: bold;
                }}
                .ability {{
                    padding: 12px 20px;
                    background: #eef2f7;
                    border-top: 1px solid #ddd;
                    font-size: 14px;
                }}
                .ability-label {{
                    font-weight: 800;
                    color: #a45d2e;
                    margin-right: 10px;
                }}
                .section-title {{
                    font-size: 20px;
                    font-weight: bold;
                    margin: 15px 0 10px;
                    padding-bottom: 5px;
                    border-bottom: 2px solid #f5af19;
                }}
                .evolution-list {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: 10px;
                    margin-bottom: 15px;
                }}
                .evolution-item {{
                    background: #f0f0f0;
                    padding: 5px 12px;
                    border-radius: 20px;
                    font-size: 14px;
                }}
                .skills-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
                    gap: 8px;
                    margin-bottom: 15px;
                }}
                .skill-item {{
                    background: #f9f9f9;
                    border-radius: 12px;
                    padding: 8px;
                    font-size: 13px;
                }}
                .skill-name {{
                    font-weight: bold;
                }}
                .skill-meta {{
                    display: flex;
                    align-items: center;
                    gap: 5px;
                    margin-top: 4px;
                    font-size: 12px;
                }}
                .skill-type-icon, .skill-cat-icon {{
                    width: 20px;
                    height: 20px;
                }}
                .typechart-grid {{
                    display: flex;
                    gap: 20px;
                    flex-wrap: wrap;
                    margin-bottom: 20px;
                }}
                .typechart-col {{
                    flex: 1;
                    background: #f5f5f5;
                    padding: 10px;
                    border-radius: 12px;
                }}
                .subtitle {{
                    font-weight: bold;
                    margin-bottom: 8px;
                    color: #333;
                }}
                .damage-item {{
                    margin: 5px 0;
                }}
                .badge-2x {{
                    background: #fd7e14;
                    color: white;
                    padding: 2px 6px;
                    border-radius: 12px;
                    font-size: 12px;
                    margin-right: 8px;
                }}
                .badge-half {{
                    background: #20c997;
                    color: white;
                    padding: 2px 6px;
                    border-radius: 12px;
                    font-size: 12px;
                    margin-right: 8px;
                }}
                .footer {{
                    background: #ffd966;
                    text-align: center;
                    padding: 8px;
                    font-size: 12px;
                    color: #5a3e1b;
                }}
            </style>
            <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
        </head>
        <body>
            <div class="card">
                <div class="card-header">
                    <span class="pokemon-name">{data['name']}</span>
                    <span class="pokemon-number">{data['number']}</span>
                </div>
                <div class="avatar-area">
                    <img class="avatar" src="{avatar_src}" 
                         referrerpolicy="no-referrer"
                         onerror="this.onerror=null;this.src='https://via.placeholder.com/150?text=LoadFailed';">
                </div>
                <div class="attr-area">
                    {attr_html}
                </div>
                <div class="stats-container">
                    {stats_html}
                </div>
                <div class="ability">
                    <span class="ability-label">✨ 特性</span> {data['ability'] or '无'}
                </div>
                {evolution_html}
                {skills_html}
                {type_chart_html}
                <div class="footer">
                    洛克王国 Wiki 数据 | t_id: {t_id}
                </div>
            </div>
        </body>
        </html>
        """
        
        html_page = await self._context.new_page()
        try:
            await html_page.set_content(html_content, wait_until="commit", timeout=20000)
            await html_page.wait_for_timeout(800)
            card_element = await html_page.query_selector(".card")
            if card_element:
                await card_element.screenshot(path=str(cache_path))
            else:
                await html_page.screenshot(path=str(cache_path), full_page=True)
            return cache_path
        except Exception as e:
            raise Exception(f"精灵截图生成失败 (t_id={t_id}): {e}")
        finally:
            await html_page.close()

    async def _generate_skill_screenshot(self, skill_id: str, data: dict, cache_path: Path) -> Path:
        """内部方法：生成技能截图"""
        await self._ensure_browser()
        def build_skill_html(data: dict, skill_id: str) -> str:
            category_html = ''
            if data.get('categoryIcon'):
                category_html = f'<img src="{data["categoryIcon"]}" class="stat-icon" referrerpolicy="no-referrer" onerror="this.style.display=\'none\'">'
                if data.get('categoryName'):
                    category_html += f'<span class="stat-name">{data["categoryName"]}</span>'
            else:
                category_html = data.get('categoryName') or '—'
            
            type_html = ''
            if data.get('typeIcon'):
                type_html = f'<img src="{data["typeIcon"]}" class="stat-icon" referrerpolicy="no-referrer" onerror="this.style.display=\'none\'">'
                if data.get('typeName'):
                    type_html += f'<span class="stat-name">{data["typeName"]}</span>'
            else:
                type_html = data.get('typeName') or '—'
            
            acquire_html = ''
            if data.get('acquireInfo'):
                acquire_html = f'''
                <div class="acquire-section">
                    <div class="section-title">📦 获取方式</div>
                    <div class="acquire-content">{data["acquireInfo"]}</div>
                </div>
                '''
            
            compatible_html = ''
            if data.get('compatiblePokemons') and len(data['compatiblePokemons']) > 0:
                pokemon_items = []
                for p in data['compatiblePokemons'][:50]:
                    avatar_src = p.get('avatar', '') or 'https://via.placeholder.com/40?text=No+Img'
                    pokemon_items.append(f'''
                    <div class="compatible-pokemon">
                        <img class="pokemon-avatar" src="{avatar_src}" referrerpolicy="no-referrer" onerror="this.src='https://via.placeholder.com/40?text=?'">
                        <span class="pokemon-name">{p['name']}</span>
                    </div>
                    ''')
                compatible_html = f'''
                <div class="compatible-section">
                    <div class="section-title">🐉 适用精灵</div>
                    <div class="compatible-list">
                        {''.join(pokemon_items)}
                    </div>
                </div>
                '''
            
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>{data['name']} - 技能详情</title>
                <style>
                    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                    body {{
                        background: linear-gradient(145deg, #1a2a3a 0%, #0f1a24 100%);
                        padding: 20px;
                        font-family: 'Segoe UI', 'PingFang SC', Roboto, 'Microsoft YaHei', sans-serif;
                        margin: 0;
                    }}
                    .skill-card {{
                        width: 100%;
                        max-width: 600px;
                        margin: 0 auto;
                        background: #ffffff;
                        border-radius: 36px;
                        box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);
                        overflow: hidden;
                    }}
                    .skill-header {{
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        padding: 24px 28px;
                        color: white;
                        display: flex;
                        align-items: center;
                        gap: 20px;
                    }}
                    .skill-icon {{
                        width: 80px;
                        height: 80px;
                        object-fit: contain;
                        background: rgba(255,255,255,0.2);
                        border-radius: 20px;
                        padding: 8px;
                    }}
                    .skill-name-box {{
                        flex: 1;
                    }}
                    .skill-name {{
                        font-size: 28px;
                        font-weight: 800;
                        letter-spacing: 1px;
                        margin-bottom: 8px;
                    }}
                    .skill-id {{
                        font-size: 13px;
                        opacity: 0.8;
                        background: rgba(255,255,255,0.2);
                        display: inline-block;
                        padding: 4px 12px;
                        border-radius: 40px;
                    }}
                    .skill-content {{
                        padding: 24px 28px;
                    }}
                    .stats-grid {{
                        display: grid;
                        grid-template-columns: repeat(4, 1fr);
                        gap: 12px;
                        margin-bottom: 24px;
                        background: #f8f9fc;
                        border-radius: 24px;
                        padding: 16px;
                    }}
                    .stat-item {{
                        text-align: center;
                    }}
                    .stat-label {{
                        font-size: 12px;
                        color: #6c757d;
                        margin-bottom: 8px;
                    }}
                    .stat-value {{
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        gap: 6px;
                        font-size: 20px;
                        font-weight: bold;
                        color: #2c3e4e;
                    }}
                    .stat-icon {{
                        width: 28px;
                        height: 28px;
                        object-fit: contain;
                    }}
                    .stat-name {{
                        font-size: 14px;
                    }}
                    .description-section {{
                        background: #fef7e0;
                        border-radius: 20px;
                        padding: 16px;
                        margin-bottom: 20px;
                        border-left: 4px solid #f5af19;
                    }}
                    .section-title {{
                        font-weight: 800;
                        font-size: 18px;
                        color: #b45f2b;
                        margin-bottom: 12px;
                        display: flex;
                        align-items: center;
                        gap: 8px;
                    }}
                    .description-text {{
                        line-height: 1.5;
                        color: #2c3e4e;
                        font-size: 14px;
                    }}
                    .acquire-section {{
                        background: #e8f5e9;
                        border-radius: 20px;
                        padding: 16px;
                        margin-bottom: 20px;
                    }}
                    .acquire-content {{
                        font-size: 13px;
                        color: #2e7d32;
                    }}
                    .compatible-section {{
                        background: #e3f2fd;
                        border-radius: 20px;
                        padding: 16px;
                        margin-bottom: 16px;
                    }}
                    .compatible-list {{
                        display: flex;
                        flex-wrap: wrap;
                        gap: 12px;
                        max-height: none;
                        overflow-y: visible;
                    }}
                    .compatible-pokemon {{
                        display: flex;
                        flex-direction: column;
                        align-items: center;
                        width: 80px;
                        background: white;
                        border-radius: 12px;
                        padding: 8px 4px;
                        box-shadow: 0 2px 6px rgba(0,0,0,0.1);
                    }}
                    .pokemon-avatar {{
                        width: 48px;
                        height: 48px;
                        object-fit: contain;
                        margin-bottom: 4px;
                    }}
                    .pokemon-name {{
                        font-size: 12px;
                        text-align: center;
                        word-break: break-word;
                        color: #0d47a1;
                    }}
                    .footer {{
                        background: #f0f2f5;
                        text-align: center;
                        padding: 14px;
                        font-size: 12px;
                        color: #6c757d;
                        border-top: 1px solid #e0e0e0;
                    }}
                    @media (max-width: 550px) {{
                        .skill-card {{ margin: 10px; }}
                        .skill-header {{ flex-direction: column; text-align: center; }}
                        .stats-grid {{ grid-template-columns: repeat(2, 1fr); }}
                        .compatible-pokemon {{ width: 60px; }}
                        .pokemon-avatar {{ width: 40px; height: 40px; }}
                    }}
                </style>
            </head>
            <body>
                <div class="skill-card">
                    <div class="skill-header">
                        <img class="skill-icon" src="{data.get('icon', '')}" referrerpolicy="no-referrer" onerror="this.src='https://via.placeholder.com/80?text=No+Image'">
                        <div class="skill-name-box">
                            <div class="skill-name">{data['name']}</div>
                            <div class="skill-id">技能 ID: {skill_id}</div>
                        </div>
                    </div>
                    <div class="skill-content">
                        <div class="stats-grid">
                            <div class="stat-item">
                                <div class="stat-label">耗能</div>
                                <div class="stat-value">⭐ {data.get('energy', '—')}</div>
                            </div>
                            <div class="stat-item">
                                <div class="stat-label">分类</div>
                                <div class="stat-value">{category_html}</div>
                            </div>
                            <div class="stat-item">
                                <div class="stat-label">属性</div>
                                <div class="stat-value">{type_html}</div>
                            </div>
                            <div class="stat-item">
                                <div class="stat-label">威力</div>
                                <div class="stat-value">{data.get('power', '—')}</div>
                            </div>
                        </div>
                        <div class="description-section">
                            <div class="section-title">✨ 技能效果</div>
                            <div class="description-text">{data.get('description', '无描述')}</div>
                        </div>
                        {acquire_html}
                        {compatible_html}
                    </div>
                    <div class="footer">洛克王国 Wiki 技能数据库 | 数据仅供参考</div>
                </div>
            </body>
            </html>
            """
        
        html_page = await self._context.new_page()
        try:
            html_content = build_skill_html(data, skill_id)
            await html_page.set_content(html_content, wait_until="commit", timeout=20000)
            await html_page.wait_for_timeout(1000)
            await html_page.set_viewport_size({"width": 600, "height": 1})
            await html_page.screenshot(path=str(cache_path), full_page=True)
            return cache_path
        finally:
            await html_page.close()

    async def _generate_team_screenshot(self, team_id: str, data: dict, cache_path: Path) -> Path:
        """内部方法：生成配队截图"""
        await self._ensure_browser()
        def build_team_html(data: dict, team_id: str) -> str:
            pokemons_html = ""
            for p in data.get('pokemons', []):
                base_stats_html = "<div class='stats-subsection'><div class='subtitle'>种族值</div><div class='stats-grid'>"
                for stat, val in p.get('base_stats', {}).items():
                    base_stats_html += f"<div class='stat-item'><span class='stat-label'>{stat}</span><span class='stat-value'>{val}</span></div>"
                base_stats_html += "</div></div>"
                pvp_stats_html = "<div class='stats-subsection'><div class='subtitle'>PVP属性值</div><div class='stats-grid'>"
                for stat, val in p.get('pvp_stats', {}).items():
                    pvp_stats_html += f"<div class='stat-item'><span class='stat-label'>{stat}</span><span class='stat-value'>{val}</span></div>"
                pvp_stats_html += "</div></div>"
                skills_html = "<div class='skills-subsection'><div class='subtitle'>技能配置</div><div class='skills-list'>"
                for skill in p.get('skills', []):
                    skills_html += f"<span class='skill-badge'>{skill}</span>"
                skills_html += "</div></div>"
                pokemons_html += f"""
                <div class="pokemon-detail-card">
                    <div class="pokemon-header">
                        <img class="pokemon-avatar" src="{p.get('avatar', '')}" referrerpolicy="no-referrer" onerror="this.src='https://via.placeholder.com/80'">
                        <div class="pokemon-name">{p.get('name', '未知')}</div>
                    </div>
                    <div class="pokemon-stats">
                        {base_stats_html}
                        {pvp_stats_html}
                        {skills_html}
                    </div>
                </div>
                """
            analysis = data.get('type_analysis', {})
            analysis_html = f"""
            <div class="analysis-section">
                <div class="section-title">阵容属性分析</div>
                <div class="analysis-content">
                    <div class="advantage"><strong>优势打击面：</strong> {analysis.get('advantage', '无')}</div>
                    <div class="weakness"><strong>阵容弱点：</strong> {analysis.get('weakness', '无')}</div>
                </div>
            </div>
            """
            trainer_skill_html = ""
            if data.get('trainer_skill'):
                trainer_skill_html = f"""
                <div class="trainer-skill-section">
                    <div class="section-title">训练师技能</div>
                    <div class="skill-content">{data['trainer_skill']}</div>
                </div>
                """
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>{data.get('team_name', '配队')} - 洛克王国配队详情</title>
                <style>
                    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                    body {{
                        background: #1a2a3a;
                        padding: 20px;
                        font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
                    }}
                    .team-container {{
                        max-width: 800px;
                        margin: 0 auto;
                        background: white;
                        border-radius: 32px;
                        overflow: hidden;
                        box-shadow: 0 20px 40px rgba(0,0,0,0.3);
                    }}
                    .team-header {{
                        background: linear-gradient(135deg, #11998e, #38ef7d);
                        padding: 24px;
                        color: white;
                    }}
                    .team-name {{
                        font-size: 28px;
                        font-weight: 800;
                        margin-bottom: 8px;
                    }}
                    .team-desc {{
                        font-size: 14px;
                        opacity: 0.9;
                        margin-top: 8px;
                    }}
                    .team-id {{
                        font-size: 12px;
                        background: rgba(0,0,0,0.2);
                        display: inline-block;
                        padding: 4px 12px;
                        border-radius: 20px;
                        margin-top: 8px;
                    }}
                    .section-title {{
                        font-size: 20px;
                        font-weight: bold;
                        margin: 20px 0 15px;
                        padding-bottom: 5px;
                        border-bottom: 2px solid #f5af19;
                    }}
                    .pokemons-grid {{
                        display: grid;
                        gap: 20px;
                        padding: 0 20px;
                    }}
                    .pokemon-detail-card {{
                        background: #f8f9fc;
                        border-radius: 24px;
                        overflow: hidden;
                        margin-bottom: 16px;
                        box-shadow: 0 2px 8px rgba(0,0,0,0.05);
                    }}
                    .pokemon-header {{
                        display: flex;
                        align-items: center;
                        gap: 15px;
                        background: #eef2f7;
                        padding: 12px 20px;
                    }}
                    .pokemon-avatar {{
                        width: 60px;
                        height: 60px;
                        object-fit: contain;
                        background: white;
                        border-radius: 50%;
                        padding: 5px;
                    }}
                    .pokemon-name {{
                        font-size: 20px;
                        font-weight: 700;
                        color: #2c3e4e;
                    }}
                    .pokemon-stats {{
                        padding: 16px;
                        display: flex;
                        flex-wrap: wrap;
                        gap: 16px;
                    }}
                    .stats-subsection {{
                        flex: 1;
                        min-width: 150px;
                    }}
                    .subtitle {{
                        font-weight: 600;
                        margin-bottom: 8px;
                        color: #a45d2e;
                    }}
                    .stats-grid {{
                        display: grid;
                        grid-template-columns: repeat(2, 1fr);
                        gap: 6px;
                        font-size: 13px;
                    }}
                    .stat-item {{
                        display: flex;
                        justify-content: space-between;
                        background: white;
                        padding: 4px 8px;
                        border-radius: 8px;
                    }}
                    .skills-list {{
                        display: flex;
                        flex-wrap: wrap;
                        gap: 8px;
                        margin-top: 8px;
                    }}
                    .skill-badge {{
                        background: #3b9eff;
                        color: white;
                        padding: 4px 10px;
                        border-radius: 20px;
                        font-size: 12px;
                    }}
                    .analysis-section, .trainer-skill-section {{
                        margin: 20px;
                        padding: 16px;
                        background: #fef7e0;
                        border-radius: 20px;
                    }}
                    .analysis-content {{
                        display: flex;
                        flex-direction: column;
                        gap: 8px;
                    }}
                    .footer {{
                        background: #f0f2f5;
                        text-align: center;
                        padding: 14px;
                        font-size: 12px;
                        color: #6c757d;
                        margin-top: 20px;
                    }}
                </style>
            </head>
            <body>
                <div class="team-container">
                    <div class="team-header">
                        <div class="team-name">{data.get('team_name', '未知名队伍')}</div>
                        <div class="team-desc">{data.get('description', '')}</div>
                        <div class="team-id">队伍 ID: {team_id}</div>
                    </div>
                    <div class="section-title">🐉 队伍成员</div>
                    <div class="pokemons-grid">
                        {pokemons_html}
                    </div>
                    {analysis_html}
                    {trainer_skill_html}
                    <div class="footer">洛克王国 Wiki 配队数据 | 仅供参考</div>
                </div>
            </body>
            </html>
            """
        
        html_page = await self._context.new_page()
        try:
            html_content = build_team_html(data, team_id)
            await html_page.set_content(html_content, wait_until="commit", timeout=20000)
            await html_page.wait_for_timeout(1000)
            await html_page.set_viewport_size({"width": 800, "height": 1})
            await html_page.screenshot(path=str(cache_path), full_page=True)
            return cache_path
        finally:
            await html_page.close()

    async def close(self):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()