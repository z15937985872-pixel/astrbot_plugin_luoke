import re
from typing import List, Dict, Any
from astrbot.api.message_components import Node, Nodes, Plain

def format_elf_text(data: Dict) -> str:
    """从 data_json 生成精灵文本"""
    name = data.get('name', '未知')
    number = data.get('number', '')
    types = data.get('types', [])
    stats = data.get('stats', {})
    ability = data.get('ability', '无')
    skills = data.get('skills', {}).get('moves', [])[:8]
    desc = data.get('description', '')
    lines = [f"【精灵】{name}"]
    if number:
        lines.append(f"编号：{number}")
    if types:
        lines.append(f"属性：{'/'.join(types)}")
    if stats:
        stat_line = ' '.join([f"{k}{v}" for k,v in stats.items() if v])
        lines.append(f"种族值：{stat_line}")
    lines.append(f"特性：{ability}")
    if skills:
        lines.append(f"技能：{'、'.join(skills)}")
    if desc:
        lines.append(f"描述：{desc[:200]}")
    return '\n'.join(lines)

def format_skill_text(data: Dict) -> str:
    # 根据实际 data 结构实现，示例简化
    name = data.get('name', '未知')
    energy = data.get('energy', '?')
    category = data.get('categoryName', '?')
    ptype = data.get('typeName', '?')
    power = data.get('power', '?')
    desc = data.get('description', '无描述')
    lines = [
        f"【技能】{name}",
        f"耗能：{energy}  分类：{category}  属性：{ptype}  威力：{power}",
        f"效果：{desc[:200]}"
    ]
    if data.get('acquireInfo'):
        lines.append(f"获取：{data['acquireInfo'][:100]}")
    return '\n'.join(lines)

def format_team_text(data: Dict) -> str:
    team_name = data.get('team_name', '未知名队伍')
    desc = data.get('description', '')
    pokemons = data.get('pokemons', [])
    lines = [f"【配队】{team_name}"]
    if desc:
        lines.append(f"简介：{desc}")
    lines.append("成员：")
    for p in pokemons:
        name = p.get('name', '未知')
        skills = p.get('skills', [])
        skill_str = '、'.join(skills[:3]) + ('...' if len(skills)>3 else '')
        lines.append(f"  - {name}（技能：{skill_str}）")
    analysis = data.get('type_analysis', {})
    if analysis.get('advantage'):
        lines.append(f"优势打击：{analysis['advantage']}")
    if analysis.get('weakness'):
        lines.append(f"阵容弱点：{analysis['weakness']}")
    return '\n'.join(lines)

def format_egg_group_text(name: str, egg_group: str, count: int, cannot_breed: bool) -> str:
    lines = [f"🥚 {name} 的蛋组：{egg_group}"]
    if cannot_breed:
        lines.append("⚠️ 该精灵无法繁殖")
    else:
        lines.append(f"同蛋组精灵数量：{count}")
    return '\n'.join(lines)

def format_breeding_plan_text(result: Dict, plan: Dict, gender: str) -> str:
    parent = result.get('parent_pokemon', {})
    target = result.get('target_pokemon', {})
    lines = [
        f"✨ 已知精灵：{parent.get('name', '?')}（{'♂' if gender=='male' else '♀'}）",
        f"🎯 目标精灵：{target.get('name', '?')}",
        f"📊 需要 {plan.get('steps', 0)} 代{'（直接生蛋）' if plan.get('type') == 'direct' else '（多代孵蛋）'}",
        ""
    ]
    for step in plan.get('plan', []):
        p1 = step['parent1']['name']
        p2 = step['parent2']['name']
        p1_gender = '♂' if step['parent1_gender'] == 'male' else '♀'
        p2_gender = '♂' if step['parent2_gender'] == 'male' else '♀'
        res = step['result']['name']
        res_gender = '♂' if step['result_gender'] == 'male' else '♀'
        note = step.get('note', '')
        line = f"{step['step']}. {p1}({p1_gender}) ❤️ {p2}({p2_gender}) → {res}({res_gender})"
        if note:
            line += f"（{note}）"
        lines.append(line)
    lines.append("")
    lines.append("⚠️ 注意：孵蛋并非100%成功，子代性别随机。")
    return '\n'.join(lines)

def split_long_message(text: str, max_length: int) -> List[str]:
    """将长文本拆分为多个片段，尽量在换行处分割，若单行超长则强制截断"""
    if len(text) <= max_length:
        return [text]
    chunks = []
    lines = text.splitlines()
    current = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        # 如果单行本身就超过 max_length，强制切割
        if len(line) > max_length:
            # 先保存当前累积的块
            if current:
                chunks.append('\n'.join(current))
                current = []
                current_len = 0
            # 强制切割这一长行
            for i in range(0, len(line), max_length):
                chunks.append(line[i:i+max_length])
            continue
        if current_len + line_len > max_length and current:
            chunks.append('\n'.join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append('\n'.join(current))
    return chunks

def supports_forward(event, platforms: set) -> bool:
    platform = (event.get_platform_name() or "").lower()
    return platform in platforms

async def build_forward_message(event, title: str, content: str, threshold: int = 900) -> Any:
    """如果内容超过阈值且平台支持，返回 Nodes 合并消息；否则返回 Plain"""
    if len(content) <= threshold or not supports_forward(event, {"aiocqhttp", "onebot"}):
        return Plain(content)
    nodes = [
        Node(uin=event.get_self_id(), name="洛克百科", content=[Plain(f"{title}\n\n{content}")])
    ]
    return Nodes(nodes)