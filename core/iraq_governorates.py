"""
Fixed list of Iraq's 18 governorates (المحافظات), each with an approximate
center coordinate used to recenter the map when a user searches/selects a
governorate. This is the single source of truth for governorate names —
`Location.state` is constrained to these values (see core/models.py) so
that item/store search by governorate is a reliable exact match instead of
free-text guessing.
"""

IRAQ_GOVERNORATES = [
    {'name': 'بغداد', 'lat': 33.3152, 'lng': 44.3661},
    {'name': 'البصرة', 'lat': 30.5085, 'lng': 47.7804},
    {'name': 'نينوى', 'lat': 36.3350, 'lng': 43.1189},
    {'name': 'أربيل', 'lat': 36.1911, 'lng': 44.0093},
    {'name': 'النجف', 'lat': 31.9960, 'lng': 44.3095},
    {'name': 'كربلاء', 'lat': 32.6160, 'lng': 44.0249},
    {'name': 'كركوك', 'lat': 35.4681, 'lng': 44.3922},
    {'name': 'الأنبار', 'lat': 33.4207, 'lng': 43.3000},
    {'name': 'بابل', 'lat': 32.4637, 'lng': 44.4194},
    {'name': 'ذي قار', 'lat': 31.0578, 'lng': 46.2580},
    {'name': 'ديالى', 'lat': 33.7500, 'lng': 44.6373},
    {'name': 'واسط', 'lat': 32.5122, 'lng': 45.8069},
    {'name': 'ميسان', 'lat': 31.8354, 'lng': 47.1443},
    {'name': 'المثنى', 'lat': 31.3234, 'lng': 45.2949},
    {'name': 'القادسية', 'lat': 31.9890, 'lng': 44.9247},
    {'name': 'صلاح الدين', 'lat': 34.6081, 'lng': 43.6783},
    {'name': 'دهوك', 'lat': 36.8617, 'lng': 42.9951},
    {'name': 'السليمانية', 'lat': 35.5650, 'lng': 45.4347},
]

IRAQ_GOVERNORATE_CHOICES = [(g['name'], g['name']) for g in IRAQ_GOVERNORATES]
