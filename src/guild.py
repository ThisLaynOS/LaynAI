import re

ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5
MANAGE_CHANNELS = 1 << 4
COLOR_PREFIX = '🎨 '


def parse_hex(value):
    if not isinstance(value, str):
        return None
    raw = value.strip().lstrip('#').lower()
    if re.fullmatch(r'[0-9a-f]{3}', raw):
        raw = ''.join(ch * 2 for ch in raw)
    if not re.fullmatch(r'[0-9a-f]{6}', raw):
        return None
    return raw, int(raw, 16)


def member_is_admin(permissions, *, owner=False):
    if owner:
        return True
    if permissions is None or permissions == '':
        return False
    try:
        bits = int(permissions)
    except (TypeError, ValueError):
        return False
    return bool(bits & (ADMINISTRATOR | MANAGE_CHANNELS))


def color_role_name(hex_code):
    text = hex_code if str(hex_code).startswith('#') else f'#{str(hex_code).upper()}'
    return f'{COLOR_PREFIX}{text}'


def is_color_role(role):
    name = (role or {}).get('name') or ''
    return bool(re.fullmatch(re.escape(COLOR_PREFIX) + r'#[0-9A-Fa-f]{6}', name)) and not role.get('managed') and str(role.get('permissions', '0')) == '0'


async def event_is_admin(discord, event):
    guild_id = event.get('guild_id')
    user_id = _user_id(event)
    if not guild_id or not user_id:
        return False
    member = event.get('member') or {}
    if member_is_admin(member.get('permissions')):
        return True
    try:
        guild = await discord.api(f'/guilds/{guild_id}')
        if str((guild or {}).get('owner_id')) == str(user_id):
            return True
        mem = await discord.api(f'/guilds/{guild_id}/members/{user_id}')
        roles = await discord.api(f'/guilds/{guild_id}/roles')
    except Exception:
        return False
    by_id = {r['id']: r for r in (roles or [])}
    bits = 0
    everyone = by_id.get(str(guild_id))
    if everyone:
        bits |= int(everyone.get('permissions') or 0)
    role_ids = (mem or {}).get('roles') or member.get('roles') or []
    for role_id in role_ids:
        role = by_id.get(role_id)
        if not role:
            continue
        bits |= int(role.get('permissions') or 0)
    return member_is_admin(bits, owner=False)


def _user_id(event):
    member = event.get('member') or {}
    user = member.get('user') or event.get('user') or event.get('author') or {}
    return user.get('id')
