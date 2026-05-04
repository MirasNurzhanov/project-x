from django.db import migrations

INTERESTS = [
    # Music
    {"name": "Music",           "icon": "🎵"},
    {"name": "Guitar",          "icon": "🎸"},
    {"name": "DJing",           "icon": "🎧"},
    {"name": "Singing",         "icon": "🎤"},
    # Sports
    {"name": "Football",        "icon": "⚽"},
    {"name": "Basketball",      "icon": "🏀"},
    {"name": "Tennis",          "icon": "🎾"},
    {"name": "Swimming",        "icon": "🏊"},
    {"name": "Cycling",         "icon": "🚴"},
    {"name": "Skateboarding",   "icon": "🛹"},
    # Gaming
    {"name": "Gaming",          "icon": "🎮"},
    {"name": "Esports",         "icon": "🏆"},
    {"name": "Board Games",     "icon": "♟️"},
    # Art
    {"name": "Drawing",         "icon": "✏️"},
    {"name": "Painting",        "icon": "🎨"},
    {"name": "Sculpting",       "icon": "🗿"},
    {"name": "Street Art",      "icon": "🖌️"},
    # Technology
    {"name": "Coding",          "icon": "💻"},
    {"name": "AI & Machine Learning", "icon": "🤖"},
    {"name": "Robotics",        "icon": "🦾"},
    {"name": "3D Printing",     "icon": "🖨️"},
    # Travel
    {"name": "Travel",          "icon": "✈️"},
    {"name": "Hiking",          "icon": "🥾"},
    {"name": "Backpacking",     "icon": "🎒"},
    {"name": "Road Trips",      "icon": "🚗"},
    # Food
    {"name": "Cooking",         "icon": "🍳"},
    {"name": "Baking",          "icon": "🍞"},
    {"name": "Coffee",          "icon": "☕"},
    {"name": "Street Food",     "icon": "🌮"},
    # Fitness
    {"name": "Gym & Weightlifting", "icon": "🏋️"},
    {"name": "Yoga",            "icon": "🧘"},
    {"name": "Running",         "icon": "🏃"},
    {"name": "Martial Arts",    "icon": "🥋"},
    # Photography
    {"name": "Photography",     "icon": "📷"},
    {"name": "Videography",     "icon": "🎬"},
    {"name": "Film Photography","icon": "🎞️"},
    # Movies & TV
    {"name": "Movies",          "icon": "🎥"},
    {"name": "Anime",           "icon": "🌸"},
    {"name": "Documentaries",   "icon": "📽️"},
]


def seed_interests(apps, schema_editor):
    Interest = apps.get_model("interests", "Interest")
    for item in INTERESTS:
        Interest.objects.get_or_create(name=item["name"], defaults={"icon": item["icon"]})


def unseed_interests(apps, schema_editor):
    Interest = apps.get_model("interests", "Interest")
    Interest.objects.filter(name__in=[i["name"] for i in INTERESTS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("interests", "0002_interest_icon"),
    ]

    operations = [
        migrations.RunPython(seed_interests, reverse_code=unseed_interests),
    ]
