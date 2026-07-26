"""Quest content catalogue.

Every quest is authored as a *template* keyed by category, then materialised
per interest. Three reasons it works this way rather than as 780 hand-written
rows:

* interests are not a fixed set — the 305-interest seed and user-created
  "Other" interests both add rows after this migration runs, and templates
  let `sync_quests` generate their quests too;
* the same wording stays consistent across sibling interests, so moderators
  judge like against like;
* every string here is a format template with named fields, so translating
  later means translating ~230 templates rather than thousands of rows.

Translation contract: each generated Quest stores `template_key` (e.g.
`daily.music.instrument_ready`) and `template_params` (e.g.
`{"interest": "Guitar"}`). English text lives in title/description as the
fallback; an i18n layer can render the key with the params instead.

Authoring rule for every line below: a moderator looking at ONE photo must be
able to tell whether it was done. Nothing about intent, plans, or feelings.
"""

# ── Points ────────────────────────────────────────────────────────────────────
# Daily quests are a single photo of something you did today: small.
# Weekly quests are multi-step, social, or take real planning: much larger.
DAILY_POINTS_LIGHT = 15
DAILY_POINTS_STANDARD = 25
DAILY_POINTS_EFFORT = 40

WEEKLY_POINTS_STANDARD = 150
WEEKLY_POINTS_EFFORT = 250

# How many daily quests each user is offered per day, drawn from their pool.
# Fixed for everyone so the weekly leaderboard stays fair regardless of how
# many interests a user selected.
DAILY_OFFER_COUNT = 3


def _d(key, title, description, points=DAILY_POINTS_STANDARD):
    return {'key': key, 'title': title, 'description': description, 'points': points}


# ── Daily templates, by category ──────────────────────────────────────────────
# {interest} is substituted with the interest name.

MUSIC = [
    _d('instrument_ready', '{interest} session', 'Photo of your {interest} setup ready to play.'),
    _d('practice_material', "Today's practice", 'Photo of what you practised — sheet music, tabs, or your DAW on screen.'),
    _d('now_playing', 'Now playing', 'Photo of the track playing right now — screen or sleeve.', DAILY_POINTS_LIGHT),
    _d('practice_timer', 'Twenty minutes in', 'Photo of a timer or practice app showing at least 20 minutes today.'),
    _d('written_by_hand', 'Written out', 'Photo of chords or lyrics you wrote by hand today.'),
    _d('gear_care', 'Gear maintenance', 'Photo of your instrument mid-clean, restring, or setup.'),
    _d('discovery', 'New find', 'Photo of a track or artist you discovered today, on screen.', DAILY_POINTS_LIGHT),
    _d('live_music', 'Live sound', 'Photo of live music happening in front of you.', DAILY_POINTS_EFFORT),
    _d('physical_media', 'Physical copy', 'Photo of a record, CD, or tape from your collection.', DAILY_POINTS_LIGHT),
    _d('flyer', 'Gig flyer', 'Photo of a music poster or flyer you walked past today.', DAILY_POINTS_LIGHT),
    _d('playlist_built', 'Playlist built', 'Photo of a playlist you put together today, tracks visible.'),
    _d('practice_spot', 'Where you played', 'Photo of the room or spot you played {interest} in today.'),
    _d('tuner', 'In tune', 'Photo of your tuner or metronome in use.', DAILY_POINTS_LIGHT),
    _d('music_shop', 'Shop visit', 'Photo from inside a music shop.', DAILY_POINTS_EFFORT),
    _d('cover_art', 'Cover art', 'Photo of album artwork you love, physical or on screen.', DAILY_POINTS_LIGHT),
    _d('sound_system', 'Listening rig', 'Photo of the speakers or headphones you are listening on.', DAILY_POINTS_LIGHT),
    _d('theory', 'Theory work', 'Photo of a scale, chord chart, or exercise you worked through today.'),
    _d('recording', 'Take in progress', 'Photo of your recording setup with a take underway.', DAILY_POINTS_EFFORT),
    _d('venue', 'At the venue', 'Photo of the crowd or stage at a music event.', DAILY_POINTS_EFFORT),
    _d('taught_someone', 'Passed it on', 'Photo of you showing someone a {interest} technique — their hands or instrument in frame.', DAILY_POINTS_EFFORT),
]

SPORTS = [
    _d('kit_ready', 'Kit ready', 'Photo of your {interest} kit or gear laid out before a session.', DAILY_POINTS_LIGHT),
    _d('the_pitch', 'Where you played', 'Photo of the pitch, court, or track you used today.'),
    _d('post_session', 'After the session', 'Photo of yourself or your gear straight after playing {interest}.'),
    _d('tracker', 'On the clock', 'Photo of your watch or app showing today\'s {interest} activity.'),
    _d('scoreboard', 'Final score', 'Photo of the scoreboard from a game you played or watched.', DAILY_POINTS_EFFORT),
    _d('team_colours', 'Colours on', 'Photo of you wearing your team colours or club shirt.', DAILY_POINTS_LIGHT),
    _d('warmup', 'Warm-up done', 'Photo from your warm-up spot today.', DAILY_POINTS_LIGHT),
    _d('equipment_fix', 'Kit maintenance', 'Photo of you repairing or cleaning your {interest} equipment.'),
    _d('watching', 'Match day', 'Photo of a {interest} match on screen or in front of you.', DAILY_POINTS_LIGHT),
    _d('training_partner', 'Training partner', 'Photo of the person you trained {interest} with today.', DAILY_POINTS_EFFORT),
    _d('drill_setup', 'Drill set up', 'Photo of a drill or practice setup you built today — cones, nets, targets.'),
    _d('venue_new', 'Somewhere new', 'Photo of a {interest} venue you have not played at before.', DAILY_POINTS_EFFORT),
    _d('weather', 'Played through it', 'Photo showing the conditions you played {interest} in today.'),
    _d('stretching', 'Recovery', 'Photo of your stretching or recovery setup after {interest}.', DAILY_POINTS_LIGHT),
    _d('ticket', 'Ticket stub', 'Photo of a ticket or entry pass to a {interest} event.', DAILY_POINTS_EFFORT),
    _d('shoes', 'Worn in', 'Photo of the shoes or boots you play {interest} in.', DAILY_POINTS_LIGHT),
    _d('club_noticeboard', 'Club board', 'Photo of a club noticeboard, fixture list, or league table.', DAILY_POINTS_LIGHT),
    _d('hydration', 'Fuel', 'Photo of what you ate or drank to refuel after {interest} today.', DAILY_POINTS_LIGHT),
    _d('personal_best', 'New best', 'Photo of a result, time, or score that beat your previous best.', DAILY_POINTS_EFFORT),
    _d('taught_someone', 'Passed it on', 'Photo of you showing someone a {interest} skill — them in frame, mid-attempt.', DAILY_POINTS_EFFORT),
]

GAMING = [
    _d('setup', 'Battle station', 'Photo of your {interest} setup as it is right now.', DAILY_POINTS_LIGHT),
    _d('session_screen', 'On screen', 'Photo of your screen mid-session today.', DAILY_POINTS_LIGHT),
    _d('achievement', 'Unlocked', 'Photo of an achievement, trophy, or level you cleared today.'),
    _d('physical_copy', 'On the shelf', 'Photo of a physical game, box, or cartridge you own.', DAILY_POINTS_LIGHT),
    _d('controller', 'Weapon of choice', 'Photo of the controller, board, or dice you played with today.', DAILY_POINTS_LIGHT),
    _d('co_op', 'Two player', 'Photo of the person you played {interest} with today.', DAILY_POINTS_EFFORT),
    _d('table_setup', 'Table set', 'Photo of a game set up and ready to play.'),
    _d('scoresheet', 'Score sheet', 'Photo of the score sheet or final standings from a game today.'),
    _d('new_game', 'Something new', 'Photo of a game you tried for the first time today.'),
    _d('cleanup', 'Cable management', 'Photo of your setup after tidying or rebuilding it.'),
    _d('merch', 'Merch', 'Photo of {interest} merchandise you own — shirt, figure, poster.', DAILY_POINTS_LIGHT),
    _d('shop', 'Game shop', 'Photo from inside a game or hobby shop.', DAILY_POINTS_EFFORT),
    _d('character', 'Your character', 'Photo of your character, deck, or army on screen or table.', DAILY_POINTS_LIGHT),
    _d('tournament', 'Bracket', 'Photo of a tournament bracket or ladder you are in.', DAILY_POINTS_EFFORT),
    _d('stream', 'Watching the pros', 'Photo of a {interest} match or stream you watched today.', DAILY_POINTS_LIGHT),
    _d('rulebook', 'Reading the rules', 'Photo of a rulebook or guide open at the page you studied.'),
    _d('painted', 'Painted up', 'Photo of a miniature, model, or custom piece you worked on today.', DAILY_POINTS_EFFORT),
    _d('group_night', 'Games night', 'Photo of the table with everyone playing.', DAILY_POINTS_EFFORT),
    _d('retro', 'Old school', 'Photo of retro hardware or a classic game you played today.'),
    _d('taught_someone', 'Taught the rules', 'Photo of you teaching someone {interest} — them at the table or screen.', DAILY_POINTS_EFFORT),
]

ART = [
    _d('todays_work', "Today's piece", 'Photo of what you made today, finished or not.'),
    _d('workspace', 'Studio state', 'Photo of your {interest} workspace as it is right now.', DAILY_POINTS_LIGHT),
    _d('materials', 'Tools out', 'Photo of the materials you used today.', DAILY_POINTS_LIGHT),
    _d('sketch', 'Quick study', 'Photo of a sketch or study you did today.'),
    _d('progress', 'Work in progress', 'Photo of a piece part-finished, showing today\'s progress.'),
    _d('reference', 'Reference', 'Photo of the subject or reference you worked from today.'),
    _d('palette', 'Palette', 'Photo of the colours you mixed or chose today.', DAILY_POINTS_LIGHT),
    _d('outdoors', 'Drawn on location', 'Photo of your work with the real scene visible behind it.', DAILY_POINTS_EFFORT),
    _d('gallery', 'Gallery visit', 'Photo from inside a gallery or exhibition.', DAILY_POINTS_EFFORT),
    _d('street_piece', 'Out in the world', 'Photo of {interest} you spotted in public today.'),
    _d('supplies', 'Restocked', 'Photo of art supplies you bought or restocked today.', DAILY_POINTS_LIGHT),
    _d('cleanup', 'Brushes down', 'Photo of your tools cleaned and put away after a session.', DAILY_POINTS_LIGHT),
    _d('old_work', 'Then and now', 'Photo of an older piece of yours next to something recent.'),
    _d('framed', 'Mounted', 'Photo of your work framed, mounted, or hung.', DAILY_POINTS_EFFORT),
    _d('sketchbook_spread', 'Full spread', 'Photo of a full sketchbook spread you filled.'),
    _d('technique', 'New technique', 'Photo of a technique you tried for the first time today.'),
    _d('commission', 'For someone else', 'Photo of a piece you made for another person.', DAILY_POINTS_EFFORT),
    _d('book', 'Studying', 'Photo of an art book or tutorial open at what you studied today.', DAILY_POINTS_LIGHT),
    _d('with_subject', 'With the subject', 'Photo of your finished piece held up beside what it depicts.', DAILY_POINTS_EFFORT),
    _d('taught_someone', 'Passed it on', 'Photo of you showing someone a {interest} technique — their work in frame.', DAILY_POINTS_EFFORT),
]

TECH = [
    _d('screen_today', "Today's work", 'Photo of your screen showing what you built or studied today.'),
    _d('desk_setup', 'Workstation', 'Photo of your {interest} setup as it is right now.', DAILY_POINTS_LIGHT),
    _d('whiteboard', 'On the board', 'Photo of a diagram, plan, or notes you drew out today.'),
    _d('green_build', 'It builds', 'Photo of a passing build, test run, or successful deploy on screen.'),
    _d('bug_fixed', 'Bug squashed', 'Photo of the error you fixed today — before or after on screen.'),
    _d('hardware', 'Hardware', 'Photo of the device, board, or machine you worked on today.'),
    _d('notebook', 'Written down', 'Photo of handwritten notes from today\'s {interest} work.', DAILY_POINTS_LIGHT),
    _d('book_open', 'Reading up', 'Photo of a book or documentation open at what you read today.', DAILY_POINTS_LIGHT),
    _d('workshop', 'Workshop', 'Photo from a makerspace, lab, or workshop you used today.', DAILY_POINTS_EFFORT),
    _d('print_result', 'Off the printer', 'Photo of something that came off your printer or machine today.', DAILY_POINTS_EFFORT),
    _d('pair', 'Pairing', 'Photo of the person you worked on {interest} with today.', DAILY_POINTS_EFFORT),
    _d('first_run', 'First run', 'Photo of a project running for the first time.', DAILY_POINTS_EFFORT),
    _d('teardown', 'Teardown', 'Photo of something you took apart today.'),
    _d('cable_tidy', 'Tidied up', 'Photo of your setup after tidying or rewiring it.', DAILY_POINTS_LIGHT),
    _d('meetup', 'Tech meetup', 'Photo from a {interest} meetup, talk, or hackathon.', DAILY_POINTS_EFFORT),
    _d('sticker', 'Laptop lid', 'Photo of your laptop or kit and its stickers.', DAILY_POINTS_LIGHT),
    _d('old_project', 'Back to it', 'Photo of an old project you picked back up today.'),
    _d('spec_sheet', 'Specs', 'Photo of the datasheet or spec you worked from today.', DAILY_POINTS_LIGHT),
    _d('demo', 'Demoed it', 'Photo of you showing your project to someone — them in frame.', DAILY_POINTS_EFFORT),
    _d('taught_someone', 'Passed it on', 'Photo of you helping someone with {interest} — their screen in frame.', DAILY_POINTS_EFFORT),
]

TRAVEL = [
    _d('where_today', 'Where you are', 'Photo of somewhere you went today that you had not been before.'),
    _d('packed', 'Packed', 'Photo of your bag packed for a {interest} trip.', DAILY_POINTS_LIGHT),
    _d('view', 'The view', 'Photo of the view from the furthest point you reached today.'),
    _d('map', 'Route planned', 'Photo of the map or route you followed today.', DAILY_POINTS_LIGHT),
    _d('transport', 'How you got there', 'Photo of the transport you took today.', DAILY_POINTS_LIGHT),
    _d('local_food', 'Local plate', 'Photo of something local you ate on the road.'),
    _d('landmark', 'Landmark', 'Photo of a landmark or sign at the place you visited.'),
    _d('trail_marker', 'On the trail', 'Photo of a trail marker, waypoint, or distance sign you passed.'),
    _d('boots', 'Kit that carried you', 'Photo of the boots, bike, or gear that got you there.', DAILY_POINTS_LIGHT),
    _d('summit', 'Top of it', 'Photo from the summit or end point of today\'s route.', DAILY_POINTS_EFFORT),
    _d('camp', 'Camp set', 'Photo of your camp or overnight setup.', DAILY_POINTS_EFFORT),
    _d('stranger', 'Someone you met', 'Photo with a person you met on the road today.', DAILY_POINTS_EFFORT),
    _d('sunrise', 'Early or late', 'Photo of sunrise or sunset from where you travelled today.'),
    _d('ticket_stub', 'Ticket', 'Photo of a ticket, boarding pass, or entry stamp from today.', DAILY_POINTS_LIGHT),
    _d('detour', 'Wrong turn', 'Photo of somewhere you ended up by accident today.'),
    _d('journal', 'Written down', 'Photo of your travel journal entry from today.'),
    _d('weather_turn', 'Conditions', 'Photo showing the weather you travelled through today.', DAILY_POINTS_LIGHT),
    _d('souvenir', 'Brought back', 'Photo of something you brought back from a trip.', DAILY_POINTS_LIGHT),
    _d('distance', 'Distance covered', 'Photo of your tracker showing today\'s distance.'),
    _d('shared_route', 'Took someone along', 'Photo of the person you took on a {interest} route today.', DAILY_POINTS_EFFORT),
]

FOOD = [
    _d('made_today', 'Made today', 'Photo of something you made today, plated.'),
    _d('ingredients', 'Mise en place', 'Photo of your ingredients prepped before cooking.', DAILY_POINTS_LIGHT),
    _d('market', 'Market run', 'Photo from the market or shop you bought ingredients at today.'),
    _d('recipe_open', 'Following along', 'Photo of the recipe you cooked from today.', DAILY_POINTS_LIGHT),
    _d('first_attempt', 'First attempt', 'Photo of a dish you made for the first time.', DAILY_POINTS_EFFORT),
    _d('shared_meal', 'Fed someone', 'Photo of someone else eating what you made.', DAILY_POINTS_EFFORT),
    _d('workspace', 'Kitchen state', 'Photo of your bench mid-cook.', DAILY_POINTS_LIGHT),
    _d('tool', 'Tool of the trade', 'Photo of the pan, machine, or knife you used today.', DAILY_POINTS_LIGHT),
    _d('local_spot', 'Local find', 'Photo of a {interest} spot you tried today.'),
    _d('street_stall', 'Street stall', 'Photo of a street food stall and what you ordered.', DAILY_POINTS_EFFORT),
    _d('cross_section', 'Cut it open', 'Photo of your bake or dish cut through, showing the inside.'),
    _d('failed', 'Did not work', 'Photo of an attempt that went wrong today — honest ones count.'),
    _d('leftovers', 'Batch cooked', 'Photo of what you batch-cooked or stored today.', DAILY_POINTS_LIGHT),
    _d('brew', 'Brewed it', 'Photo of the coffee or drink you made today.', DAILY_POINTS_LIGHT),
    _d('technique', 'New technique', 'Photo of a technique you tried for the first time today.'),
    _d('handwritten_recipe', 'Written down', 'Photo of a recipe you wrote out by hand.', DAILY_POINTS_LIGHT),
    _d('grown', 'Home grown', 'Photo of a herb or ingredient you grew yourself.', DAILY_POINTS_EFFORT),
    _d('table_set', 'Table set', 'Photo of the table set for a meal you are serving.'),
    _d('cookbook', 'Studying', 'Photo of a cookbook open at what you read today.', DAILY_POINTS_LIGHT),
    _d('taught_someone', 'Cooked together', 'Photo of you cooking {interest} with someone — them in frame.', DAILY_POINTS_EFFORT),
]

FITNESS = [
    _d('session_done', 'Session done', 'Photo taken right after today\'s {interest} session.'),
    _d('where_trained', 'Where you trained', 'Photo of the gym, studio, or spot you used today.', DAILY_POINTS_LIGHT),
    _d('tracker', 'On the watch', 'Photo of your tracker showing today\'s {interest} numbers.'),
    _d('kit_ready', 'Kit out', 'Photo of your training kit laid out before a session.', DAILY_POINTS_LIGHT),
    _d('log', 'Logged', 'Photo of your training log with today\'s entry.'),
    _d('warmup_spot', 'Warm-up', 'Photo from your warm-up today.', DAILY_POINTS_LIGHT),
    _d('personal_best', 'New best', 'Photo of a weight, time, or rep count that beat your previous best.', DAILY_POINTS_EFFORT),
    _d('training_partner', 'Trained together', 'Photo of who you trained {interest} with today.', DAILY_POINTS_EFFORT),
    _d('outdoors', 'Outside', 'Photo of an outdoor {interest} session today.'),
    _d('early', 'Before the day started', 'Photo from a session you did before 9am.', DAILY_POINTS_EFFORT),
    _d('stretch', 'Cooled down', 'Photo of your stretch or mobility work after training.', DAILY_POINTS_LIGHT),
    _d('meal', 'Refuel', 'Photo of what you ate after training today.', DAILY_POINTS_LIGHT),
    _d('new_movement', 'New movement', 'Photo of an exercise or pose you tried for the first time.'),
    _d('class', 'Class', 'Photo from a {interest} class you attended.', DAILY_POINTS_EFFORT),
    _d('equipment', 'Equipment', 'Photo of the kit you trained with today.', DAILY_POINTS_LIGHT),
    _d('route', 'Route run', 'Photo from the route you covered today.'),
    _d('shoes', 'Worn in', 'Photo of the shoes you trained in today.', DAILY_POINTS_LIGHT),
    _d('plan', 'The plan', 'Photo of the programme or plan you followed today.', DAILY_POINTS_LIGHT),
    _d('rest_day', 'Active recovery', 'Photo of your recovery work on a rest day.', DAILY_POINTS_LIGHT),
    _d('taught_someone', 'Coached someone', 'Photo of you showing someone a {interest} movement — them mid-attempt.', DAILY_POINTS_EFFORT),
]

PHOTOGRAPHY = [
    _d('shot_today', "Today's shot", 'Photo you took today — the shot itself.'),
    _d('kit', 'Kit out', 'Photo of the camera or lens you shot with today.', DAILY_POINTS_LIGHT),
    _d('behind_scenes', 'Behind the shot', 'Photo showing your setup while shooting today.'),
    _d('golden_hour', 'Golden hour', 'Photo taken at sunrise or sunset today.'),
    _d('portrait', 'Portrait', 'Portrait you took of someone today, with their agreement.', DAILY_POINTS_EFFORT),
    _d('editing', 'On the edit', 'Photo of your screen mid-edit today.', DAILY_POINTS_LIGHT),
    _d('printed', 'Printed', 'Photo of one of your images printed on paper.', DAILY_POINTS_EFFORT),
    _d('roll', 'Film roll', 'Photo of a roll of film you shot or loaded.', DAILY_POINTS_LIGHT),
    _d('darkroom', 'Developing', 'Photo from developing or scanning your film.', DAILY_POINTS_EFFORT),
    _d('detail', 'Close up', 'Macro or close-up detail shot you took today.'),
    _d('street', 'Street', 'Street photograph you took today.'),
    _d('long_exposure', 'Long exposure', 'Long-exposure shot you took today.', DAILY_POINTS_EFFORT),
    _d('black_white', 'Monochrome', 'Black and white image you shot today.'),
    _d('one_lens', 'One lens only', 'Photo of the single lens you restricted yourself to today.', DAILY_POINTS_LIGHT),
    _d('reflection', 'Reflection', 'Shot using a reflection you found today.'),
    _d('cleaning', 'Sensor and glass', 'Photo of you cleaning your gear.', DAILY_POINTS_LIGHT),
    _d('exhibition', 'Exhibition', 'Photo from a photography exhibition you visited.', DAILY_POINTS_EFFORT),
    _d('old_photo', 'From the archive', 'Photo of an old print or negative from your archive.', DAILY_POINTS_LIGHT),
    _d('subject_met', 'Met the subject', 'Photo of someone you photographed today, with their agreement.', DAILY_POINTS_EFFORT),
    _d('taught_someone', 'Passed it on', 'Photo of you showing someone a {interest} technique — their camera in frame.', DAILY_POINTS_EFFORT),
]

FILM = [
    _d('watching_now', 'On screen', 'Photo of what you are watching right now.', DAILY_POINTS_LIGHT),
    _d('cinema', 'At the cinema', 'Photo from inside the cinema you went to today.', DAILY_POINTS_EFFORT),
    _d('ticket', 'Ticket', 'Photo of your ticket from a screening today.', DAILY_POINTS_LIGHT),
    _d('notes', 'Wrote it down', 'Photo of notes or a review you wrote after watching.'),
    _d('physical_copy', 'On the shelf', 'Photo of a DVD, Blu-ray, or box set you own.', DAILY_POINTS_LIGHT),
    _d('watch_together', 'Watched together', 'Photo of who you watched with today.', DAILY_POINTS_EFFORT),
    _d('poster', 'Poster', 'Photo of a film or series poster you passed today.', DAILY_POINTS_LIGHT),
    _d('snack', 'Setup', 'Photo of your viewing setup — screen and snacks.', DAILY_POINTS_LIGHT),
    _d('first_time', 'First watch', 'Photo of something you watched for the first time today.'),
    _d('rewatch', 'Rewatch', 'Photo of a favourite you returned to today.', DAILY_POINTS_LIGHT),
    _d('subtitles', 'Another language', 'Photo of something you watched in a language you do not speak.'),
    _d('classic', 'Older than you', 'Photo of a film made before you were born, on screen.'),
    _d('credits', 'Stayed for the credits', 'Photo of the credits rolling.', DAILY_POINTS_LIGHT),
    _d('watchlist', 'Watchlist', 'Photo of your watchlist as it stands today.', DAILY_POINTS_LIGHT),
    _d('merch', 'Merch', 'Photo of {interest} merchandise you own.', DAILY_POINTS_LIGHT),
    _d('book_version', 'Read the book', 'Photo of the book beside its adaptation.'),
    _d('festival', 'Festival', 'Photo from a film festival or special screening.', DAILY_POINTS_EFFORT),
    _d('location', 'Filming location', 'Photo of a real place you recognised from something you watched.', DAILY_POINTS_EFFORT),
    _d('director', 'Director run', 'Photo of the second film you watched by the same director.'),
    _d('recommended', 'Recommendation taken', 'Photo of what you watched on someone else\'s recommendation.'),
]

# Fallback for interests with no specific category — the 305-interest seed and
# user-created "Other" interests land here. Deliberately concrete: these must
# still be judgeable from one photo for any hobby at all.
GENERIC = [
    _d('did_today', 'Did it today', 'Photo of yourself doing {interest} today.'),
    _d('gear', 'Your kit', 'Photo of the gear or tools you use for {interest}.', DAILY_POINTS_LIGHT),
    _d('where', 'Where you do it', 'Photo of the place you do {interest}.', DAILY_POINTS_LIGHT),
    _d('result', 'What came of it', 'Photo of the result of a {interest} session today.'),
    _d('progress', 'Progress', 'Photo showing progress you made on {interest} today.'),
    _d('setup', 'Set up', 'Photo of your {interest} setup ready to go.', DAILY_POINTS_LIGHT),
    _d('with_someone', 'With someone', 'Photo of the person you did {interest} with today.', DAILY_POINTS_EFFORT),
    _d('first_time', 'Something new', 'Photo of a part of {interest} you tried for the first time.'),
    _d('notes', 'Written down', 'Photo of notes you made about {interest} today.', DAILY_POINTS_LIGHT),
    _d('learning', 'Studying it', 'Photo of a book, video, or guide about {interest} you used today.', DAILY_POINTS_LIGHT),
    _d('outdoors', 'Outside', 'Photo of {interest} happening outdoors today.'),
    _d('maintenance', 'Maintenance', 'Photo of you cleaning or repairing your {interest} equipment.'),
    _d('community', 'Others doing it', 'Photo of other people doing {interest} near you.', DAILY_POINTS_EFFORT),
    _d('shop', 'Supplies', 'Photo from a shop that sells {interest} gear.'),
    _d('early', 'Early start', 'Photo of {interest} before 9am today.', DAILY_POINTS_EFFORT),
    _d('collection', 'Collection', 'Photo of your {interest} collection or archive.', DAILY_POINTS_LIGHT),
    _d('old_new', 'Then and now', 'Photo of old {interest} work beside something recent.'),
    _d('event', 'Event', 'Photo from a {interest} event, class, or meetup.', DAILY_POINTS_EFFORT),
    _d('shared', 'Showed someone', 'Photo of you showing someone your {interest} work — them in frame.', DAILY_POINTS_EFFORT),
    _d('taught_someone', 'Passed it on', 'Photo of you teaching someone {interest} — them having a go.', DAILY_POINTS_EFFORT),
]

DAILY_BY_CATEGORY = {
    'music': MUSIC,
    'sports': SPORTS,
    'gaming': GAMING,
    'art': ART,
    'tech': TECH,
    'travel': TRAVEL,
    'food': FOOD,
    'fitness': FITNESS,
    'photography': PHOTOGRAPHY,
    'film': FILM,
    'generic': GENERIC,
}

# ── Interest → category ───────────────────────────────────────────────────────
INTEREST_CATEGORY = {
    'Music': 'music', 'Guitar': 'music', 'DJing': 'music', 'Singing': 'music',
    'Football': 'sports', 'Basketball': 'sports', 'Tennis': 'sports',
    'Swimming': 'sports', 'Cycling': 'sports', 'Skateboarding': 'sports',
    'Gaming': 'gaming', 'Esports': 'gaming', 'Board Games': 'gaming',
    'Drawing': 'art', 'Painting': 'art', 'Sculpting': 'art', 'Street Art': 'art',
    'Coding': 'tech', 'AI & Machine Learning': 'tech', 'Robotics': 'tech',
    '3D Printing': 'tech',
    'Travel': 'travel', 'Hiking': 'travel', 'Backpacking': 'travel',
    'Road Trips': 'travel',
    'Cooking': 'food', 'Baking': 'food', 'Coffee': 'food', 'Street Food': 'food',
    'Gym & Weightlifting': 'fitness', 'Yoga': 'fitness', 'Running': 'fitness',
    'Martial Arts': 'fitness',
    'Photography': 'photography', 'Videography': 'photography',
    'Film Photography': 'photography',
    'Movies': 'film', 'Anime': 'film', 'Documentaries': 'film',
}

# Keyword fallback for interests added after this file was written (the 305
# seed, user "Other" entries) so they get themed quests rather than generic
# ones wherever the name gives us a clear signal.
CATEGORY_KEYWORDS = [
    ('music', ('music', 'guitar', 'piano', 'violin', 'drum', 'sing', 'choir', 'dj', 'band', 'jazz', 'rap', 'opera', 'bass', 'synth')),
    ('sports', ('football', 'soccer', 'basketball', 'tennis', 'golf', 'cricket', 'rugby', 'hockey', 'baseball', 'volleyball', 'skate', 'surf', 'ski', 'snowboard', 'climb', 'sport')),
    ('gaming', ('gaming', 'game', 'esport', 'chess', 'puzzle', 'rpg', 'console', 'arcade')),
    ('art', ('art', 'draw', 'paint', 'sculpt', 'craft', 'pottery', 'knit', 'sew', 'calligraphy', 'design', 'origami')),
    ('tech', ('coding', 'program', 'software', 'robot', 'engineer', 'electronic', 'ai', 'data', 'machine learning', 'hardware', '3d print', 'hack')),
    ('travel', ('travel', 'hike', 'backpack', 'camp', 'road trip', 'explor', 'trek', 'sail', 'geocach')),
    ('food', ('cook', 'bak', 'food', 'coffee', 'tea', 'wine', 'beer', 'brew', 'grill', 'barbecue', 'cheese', 'chocolate')),
    ('fitness', ('gym', 'fitness', 'yoga', 'run', 'martial', 'boxing', 'pilates', 'crossfit', 'weight', 'cycl', 'swim', 'dance')),
    ('photography', ('photo', 'camera', 'video', 'film photo', 'cinemat')),
    ('film', ('movie', 'anime', 'documentar', 'cinema', 'tv', 'series', 'theatre', 'theater')),
]


def category_for(interest_name):
    """Best category for an interest name, falling back to 'generic'."""
    if interest_name in INTEREST_CATEGORY:
        return INTEREST_CATEGORY[interest_name]
    lowered = interest_name.lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(word in lowered for word in keywords):
            return category
    return 'generic'


def daily_templates_for(interest_name):
    return DAILY_BY_CATEGORY[category_for(interest_name)]


# ── Weekly templates ──────────────────────────────────────────────────────────
# Interest-agnostic on purpose: the weekly leaderboard is competitive, so every
# player must have the same weekly points available. Per-interest weeklies
# would hand someone with eight interests eight times the earning ceiling of
# someone with one. `{interest}`-style personalisation stays in the dailies,
# where the per-day offer cap keeps the ceiling equal.
WEEKLY_TEMPLATES = [
    {
        'key': 'weekly.meet_someone_new',
        'title': 'Meet someone new',
        'description': 'Meet up in person with someone you met through Aura this week. '
                       'Photo of the two of you together.',
        'points': WEEKLY_POINTS_EFFORT,
    },
    {
        'key': 'weekly.shared_interest_session',
        'title': 'Do your thing, together',
        'description': 'Spend a session on one of your interests with another person. '
                       'Photo of both of you doing it, gear or activity visible.',
        'points': WEEKLY_POINTS_EFFORT,
    },
    {
        'key': 'weekly.finish_something',
        'title': 'Finish something',
        'description': 'Complete a project you had already started. '
                       'Photo of the finished result.',
        'points': WEEKLY_POINTS_STANDARD,
    },
    {
        'key': 'weekly.somewhere_new',
        'title': 'Somewhere new',
        'description': 'Visit a place in your city you have never been to before. '
                       'Photo that clearly shows where you are.',
        'points': WEEKLY_POINTS_STANDARD,
    },
    {
        'key': 'weekly.attend_event',
        'title': 'Turn up',
        'description': 'Go to a public event, class, or meetup this week. '
                       'Photo from inside, with the venue or crowd visible.',
        'points': WEEKLY_POINTS_EFFORT,
    },
    {
        'key': 'weekly.teach_someone',
        'title': 'Teach what you know',
        'description': 'Show someone how to do something you are good at. '
                       'Photo of them having a go at it.',
        'points': WEEKLY_POINTS_EFFORT,
    },
    {
        'key': 'weekly.five_days',
        'title': 'Five days running',
        'description': 'Practise one of your interests on five separate days this week. '
                       'Photo of your log, calendar, or tracker showing all five.',
        'points': WEEKLY_POINTS_EFFORT,
    },
    {
        'key': 'weekly.make_for_someone',
        'title': 'Make it for someone else',
        'description': 'Make or do something for another person this week. '
                       'Photo of them with what you made.',
        'points': WEEKLY_POINTS_STANDARD,
    },
    {
        'key': 'weekly.outdoors_hours',
        'title': 'Out of the house',
        'description': 'Spend an afternoon on one of your interests outdoors. '
                       'Photo taken on location, outdoors, mid-activity.',
        'points': WEEKLY_POINTS_STANDARD,
    },
    {
        'key': 'weekly.document_progress',
        'title': 'Before and after',
        'description': 'Track a week of progress on one thing. '
                       'Photo showing the start and end state side by side.',
        'points': WEEKLY_POINTS_STANDARD,
    },
]


def render(template, interest_name=None):
    """(title, description, key, params) for a template + optional interest."""
    params = {'interest': interest_name} if interest_name else {}
    title = template['title'].format(**params) if params else template['title']
    description = template['description'].format(**params) if params else template['description']
    return title, description, template['key'], params


def daily_quest_specs(interest_name):
    """Every daily quest for one interest, ready to create."""
    category = category_for(interest_name)
    specs = []
    for template in DAILY_BY_CATEGORY[category]:
        full_key = f"daily.{category}.{template['key']}"
        title, description, _, params = render({**template, 'key': full_key}, interest_name)
        specs.append({
            'template_key': full_key,
            'template_params': params,
            'title': title,
            'description': description,
            'reward_points': template['points'],
        })
    return specs


def weekly_quest_specs():
    return [
        {
            'template_key': t['key'],
            'template_params': {},
            'title': t['title'],
            'description': t['description'],
            'reward_points': t['points'],
        }
        for t in WEEKLY_TEMPLATES
    ]
