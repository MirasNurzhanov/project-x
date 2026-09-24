from django.db import migrations

# Dubai Marina / JBR, researched 2026-09-24. Same rules as the Almaty seeds:
# address from the venue's (or its landlord's) own page, pin from OpenStreetMap
# or Yandex; no Google Places content, logo, partner claim or booking link.
# Checked as trading in 2026. Left out on purpose: Ain Dubai (conflicting 2026
# operating reports), TribeFit (no current evidence it is open) and EQUALIZE
# (inside Skydive Dubai, would sit on the same pin).
#
# Pins marked OSM are © OpenStreetMap contributors (ODbL).
DUBAI_MARINA_VENUES = (
    # --- Shopping mall -------------------------------------------------------
    # dubaimarinamall.com; pin OSM (the mall building).
    ('dubai-marina-mall', 'Dubai Marina Mall', 'Al Marsa Street, Dubai Marina, Dubai',
     'Shopping mall', '25.0766530', '55.1402110',
     'Shops, food and the Marina on the doorstep. Bring your club together and pick a meeting point inside.'),
    # thebeach.ae; pin OSM.
    ('the-beach-jbr', 'The Beach, JBR', 'The Walk, Jumeirah Beach Residence, Dubai',
     'Shopping mall', '25.0763000', '55.1315270',
     'Sand, sea and an open-air promenade. Gather your club by the water and add a meeting point so no one gets lost.'),
    # Bluewaters Island (Meraas); pin OSM (The Wharf retail area).
    ('the-wharf-bluewaters', 'The Wharf, Bluewaters', 'The Wharf, Bluewaters Island, Dubai',
     'Shopping mall', '25.0800160', '55.1231200',
     'A waterfront boulevard of cafes and shops across the bridge from JBR. Meet your club on the island.'),

    # --- Sport & fitness -----------------------------------------------------
    # uae.fitnessfirstme.com/clubs/dubai-marina-gate; pin Yandex org 180944234842.
    ('fitness-first-marina-gate', 'Fitness First Marina Gate', 'Marina Gate, Level P1, Dubai Marina, Dubai',
     'Sport & fitness', '25.0865100', '55.1468200',
     'Train together, not alone. Bring your club for a session and agree a meeting point at the podium.'),
    # ufcgym.me/en/dubai/jumeirah-beach-residence; pin OSM (matches Murjan 6).
    ('ufc-gym-jbr', 'UFC GYM JBR', 'Murjan 6, Jumeirah Beach Residence, Dubai',
     'Sport & fitness', '25.0822900', '55.1397980',
     'Boxing, grappling and conditioning in the heart of JBR. Turn your club into a training crew.'),
    # wellfit.me/en/marina; pin OSM (Al Fattan Business Centre).
    ('wellfit-marina', 'Wellfit Marina', '12th & 13th Floor, Al Fattan Business Hub, Dubai Marina, Dubai',
     'Sport & fitness', '25.0791890', '55.1368420',
     'Two floors of gym above the Marina. Plan a workout with your club and meet in the lobby.'),
    # Studio listings (Shop 2, Park Island); pin OSM.
    ('platform-studios-marina', 'The Platform Studios', 'Shop 2, Park Island, Dubai Marina, Dubai',
     'Sport & fitness', '25.0820160', '55.1414210',
     'Boutique classes at Park Island. Book a class with your club and grab a coffee after.'),
    # dubaimarinayachtclub.com/contact; pin OSM (clubhouse, beside Vida Marina).
    ('dubai-marina-yacht-club', 'Dubai Marina Yacht Club', 'Vida Dubai Marina & Yacht Club, Dubai Marina, Dubai',
     'Sport & fitness', '25.0725510', '55.1361780',
     'Boats, berths and sunsets on the water. Meet your club at the clubhouse and plan a day on the Marina.'),
    # skydivedubai.ae (Palm Drop Zone); pin OSM.
    ('skydive-dubai-palm', 'Skydive Dubai', 'Palm Drop Zone, Dubai Marina, Dubai',
     'Sport & fitness', '25.0902730', '55.1343110',
     'Watch the jumps over the Palm, or take one. A bold plan for a club that likes a story.'),

    # --- Beach & outdoors ----------------------------------------------------
    # Public promenade; pin OSM (Marina Walk, mid-promenade).
    ('dubai-marina-walk', 'Dubai Marina Walk', 'Marina Walk, Dubai Marina, Dubai',
     'Beach & outdoors', '25.0824170', '55.1443030',
     'Seven kilometres of waterfront path. Walk, run or ride it together and set a spot to start from.'),
    # Public beach; pin OSM (Marina Beach).
    ('jbr-beach', 'JBR Beach', 'Jumeirah Beach Residence, Dubai',
     'Beach & outdoors', '25.0807830', '55.1320970',
     'A free public beach with the skyline behind you. Bring your club for a swim and name a meeting point on the sand.'),

    # --- Restaurant & cafe ---------------------------------------------------
    # arabica.com; pin OSM.
    ('arabica-jbr', '% Arabica JBR', 'The Beach, Jumeirah Beach Residence, Dubai',
     'Restaurant & cafe', '25.0787780', '55.1339220',
     'A coffee kiosk steps from the sand. Grab a latte with your club and head for the beach.'),
    # thebeach.ae/en/outlets/starbucks-reserve; pin OSM.
    ('starbucks-reserve-the-beach', 'Starbucks Reserve', 'The Beach, Jumeirah Beach Residence, Dubai',
     'Restaurant & cafe', '25.0756230', '55.1304600',
     'Small-lot coffee by the sea. An easy first meetup for a new club.'),
    # dubaimarinamall.com (Caffe Nero, ground floor); pin OSM.
    ('caffe-nero-dubai-marina-mall', 'Caffè Nero', 'Ground Floor, Dubai Marina Mall, Dubai',
     'Restaurant & cafe', '25.0763810', '55.1402480',
     'Espresso inside Dubai Marina Mall. A quick catch-up with your club before or after shopping.'),
    # theacaispotcafe.com (Marina Gate Retail 112); pin Yandex org 131810456370.
    ('the-acai-spot-marina-gate', 'The Acai Spot', 'Marina Gate, Dubai Marina, Dubai',
     'Restaurant & cafe', '25.0863930', '55.1473620',
     'Bowls and smoothies at Marina Gate. A good refuel after a club walk or workout.'),
    # thebeach.ae/en/outlets/bake-my-day (unit 1402); pin OSM.
    ('bake-my-day-jbr', 'Bake My Day', 'Unit 1402, The Beach, Jumeirah Beach Residence, Dubai',
     'Restaurant & cafe', '25.0780970', '55.1331330',
     'Dutch bakes with a view of the wheel. Gather your club for a slow breakfast.'),
    # thebeach.ae/en/outlets/common-grounds (unit 2404); pin Yandex org 100163475138.
    ('common-grounds-jbr', 'Common Grounds', 'Unit 2404, The Beach, Jumeirah Beach Residence, Dubai',
     'Restaurant & cafe', '25.0771340', '55.1324710',
     'Specialty coffee and all-day plates facing the Gulf. Bring your club for brunch.'),
    # visitdubai.com/en/restaurants/pier-7; pin OSM.
    ('pier-7', 'Pier 7', 'Pier 7, next to Dubai Marina Mall, Dubai',
     'Restaurant & cafe', '25.0762260', '55.1388290',
     'Seven floors, seven restaurants, one view of the Marina. Let your club choose a floor.'),
    # thebeach.ae/en/outlets/eataly-at-the-beach; pin OSM.
    ('eataly-the-beach', 'Eataly', 'The Beach, Jumeirah Beach Residence, Dubai',
     'Restaurant & cafe', '25.0753210', '55.1303230',
     'Italian food and an aperitivo terrace by the sea. Round up your club for dinner.'),
    # thebeach.ae/en/outlets/eggspectation; pin OSM.
    ('eggspectation-jbr', 'Eggspectation', 'The Beach, Jumeirah Beach Residence, Dubai',
     'Restaurant & cafe', '25.0784040', '55.1328910',
     'All-day breakfast on a big terrace. A reliable pick for a weekend club brunch.'),
    # thebeach.ae/en/outlets/bosporus; pin OSM.
    ('bosporus-jbr', 'Bosporus', 'The Walk, Jumeirah Beach Residence, Dubai',
     'Restaurant & cafe', '25.0779030', '55.1330360',
     'Turkish grills and mezze on The Walk. Share a table with your club.'),
    # carlucciosme.com/locations/dubai-marina-mall; pin OSM.
    ('carluccios-dubai-marina-mall', "Carluccio's", 'Level P, Dubai Marina Mall, Dubai',
     'Restaurant & cafe', '25.0764270', '55.1396740',
     'Casual Italian with a Marina terrace. Easy to find, easy to share.'),
    # zengo-dubai.com (Le Royal Méridien); pin OSM.
    ('zengo-dubai-marina', 'Zengo', 'Le Royal Méridien Beach Resort & Spa, Al Mamsha Street, Dubai',
     'Restaurant & cafe', '25.0848160', '55.1402800',
     'Pan-Asian plates with a view of the wheel. Make a night of it with your club.'),
    # reemalbawadi.com (Marina Walk branch); pin OSM.
    ('reem-al-bawadi-marina-walk', 'Reem Al Bawadi', 'Marina Walk, Dubai Marina, Dubai',
     'Restaurant & cafe', '25.0855560', '55.1476080',
     'Lebanese classics on the Marina Walk, open late. Big tables for big clubs.'),
)


def seed_dubai_marina(apps, schema_editor):
    Venue = apps.get_model('venues', 'Venue')
    manager = Venue.objects.using(schema_editor.connection.alias)
    for slug, name, address, category, latitude, longitude, description in DUBAI_MARINA_VENUES:
        manager.get_or_create(slug=slug, defaults={
            'name': name,
            'address': address,
            'category': category,
            'description': description,
            'latitude': latitude,
            'longitude': longitude,
            'is_active': True,
        })


class Migration(migrations.Migration):
    dependencies = [('venues', '0004_seed_six_coffee_wine')]
    # Do not remove potentially edited venues or detach their meetups on rollback.
    operations = [migrations.RunPython(seed_dubai_marina, migrations.RunPython.noop)]
