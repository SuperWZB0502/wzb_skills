"""Portable light-theme colors adapted from the archived msinsight frontend.

AscendUnit.tsx uses the event name (cname only for simulation), and
colorUtils.ts sums 31 * UTF-16 code units modulo the ordered palette length.
Free has no special gray override in that path. Domain colors are the user's
reference illustrations, which also match the original light theme tokens.
"""

PALETTE = [
    ('deepBlue', '#0062DC'), ('coralRed', '#FF5432'),
    ('tealGreen', '#279C6E'), ('aquaBlue', '#34B1B9'),
    ('raspberryPink', '#D53F78'), ('vividBlue', '#0077FF'),
    ('vividRed', '#FD2F2F'), ('royalPurple', '#6037DB'),
    ('skyBlue', '#2F9CE0'), ('sunsetOrange', '#EE891D'),
    ('amethystPurple', '#AA38CE'), ('limeGreen', '#75A105'),
]
DOMAIN_COLORS = dict(PP='#279C6E', DP='#FD2F2F', EP='#0062DC',
                     CP='#FF5432', TP='#6037DB', GLOBAL='#EE891D')
DOMAIN_COLORS.update({'TP-Extend-EP':'#AA38CE','DP-Modulo-EP':'#34B1B9',
                     'DP-With-CP':'#2F9CE0','DP-With-CP-Modulo-EP':'#75A105','unresolved':'#8795A6'})


def color_config():
    return {'theme': 'light', 'rule': 'sum(31 * UTF16_code_unit(name)) % 12',
            'palette': [{'key': key, 'value': value} for key, value in PALETTE],
            'domains': DOMAIN_COLORS,
            'free_color': PALETTE[(31 * sum(map(ord, 'Free'))) % len(PALETTE)][1],
            'sources': ['AscendUnit.tsx', 'colorUtils.ts', 'utils.ts', 'light.ts'],
            'global_note': 'GLOBAL使用用户image-8.png主色RGB(238,137,29)，即#EE891D。'}
