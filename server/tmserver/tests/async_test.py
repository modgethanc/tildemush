import os
import asyncio
import json
from unittest import mock

import pytest
import websockets

from ..core import GameServer
from ..migrations import reset_db
from ..models import UserAccount, Script, GameObject, ScriptRevision
from ..world import GameWorld

@pytest.fixture(autouse=True)
def state():
    if os.environ.get('TILDEMUSH_ENV') != 'test':
        pytest.exit('Run tildemush tests with TILDEMUSH_ENV=test')

    reset_db()
    GameWorld.reset()

@pytest.fixture
def mock_logger():
    yield mock.Mock()

@pytest.fixture(autouse=True)
def start_server(event_loop, mock_logger):
    gs = GameServer(GameWorld, loop=event_loop, logger=mock_logger, port=5555)
    server_future = gs._get_ws_server()
    asyncio.ensure_future(server_future, loop=event_loop)
    yield
    server_future.ws_server.server.close()

@pytest.fixture
async def client(event_loop):
    client = await websockets.connect('ws://localhost:5555', loop=event_loop)
    yield client
    # TODO this is getting called after the server is closed :( if we can fix
    # the ordering, the client.close()s can come out of the test functions
    await client.close()

@pytest.mark.asyncio
async def test_garbage(event_loop, mock_logger, client):
    await client.send('GARBAGE')
    msg = await client.recv()
    assert msg == 'ERROR: message not understood'
    await client.close()

@pytest.mark.asyncio
async def test_ping(event_loop, mock_logger, client):
    await client.send('PING')
    msg = await client.recv()
    assert msg == 'PONG'
    await client.close()

@pytest.mark.asyncio
async def test_registration_success(event_loop, mock_logger, client):
    await client.send('REGISTER vilmibm:foobarbazquux')
    msg = await client.recv()
    assert msg == 'REGISTER OK'
    await client.close()

@pytest.mark.asyncio
async def test_registration_error(event_loop, mock_logger, client):
    await client.send('REGISTER vilmibm:foo')
    msg = await client.recv()
    assert msg == 'ERROR: password too short'
    await client.close()

@pytest.mark.asyncio
async def test_login_success(event_loop, mock_logger, client):
    await client.send('REGISTER vilmibm:foobarbazquux')
    await client.recv()
    await client.send('LOGIN vilmibm:foobarbazquux')
    msg = await client.recv()
    assert msg == 'LOGIN OK'
    await client.close()

@pytest.mark.asyncio
async def test_login_error(event_loop, mock_logger, client):
    await client.send('REGISTER vilmibm:foobarbazquux')
    await client.recv()
    await client.send('LOGIN evilmibm:foobarbazquux')
    msg = await client.recv()
    assert msg == 'ERROR: no such user'
    await client.close()

@pytest.mark.asyncio
async def test_game_command(event_loop, mock_logger, client):
    await client.send('REGISTER vilmibm:foobarbazquux')
    await client.recv()
    await client.send('LOGIN vilmibm:foobarbazquux')
    await client.recv()
    await client.recv()
    await client.send('COMMAND say hello')
    msg = await client.recv()
    assert msg == 'COMMAND OK'
    msg = await client.recv()
    assert msg == 'vilmibm says, "hello"'
    await client.close()

async def setup_user(client, username, god=False):
    await client.send('REGISTER {}:foobarbazquux'.format(username))
    await client.recv()

    if god:
        ua = UserAccount.get(UserAccount.username==username)
        ua.god = True
        ua.save()

    await client.send('LOGIN {}:foobarbazquux'.format(username))
    # once for LOGIN OK
    await client.recv()
    # once for the client state update
    await client.recv()


@pytest.mark.asyncio
async def test_announce_forbidden(event_loop, mock_logger, client):
    await setup_user(client, 'vilmibm')
    await client.send('COMMAND announce HELLO EVERYONE')
    msg = await client.recv()
    assert msg == 'ERROR: you are not powerful enough to do that.'
    await client.close()

@pytest.mark.asyncio
async def test_announce(event_loop, mock_logger, client):
    await setup_user(client, 'vilmibm', god=True)
    snoozy_client = await websockets.connect('ws://localhost:5555', loop=event_loop)
    await setup_user(snoozy_client, 'snoozy')
    await client.send('COMMAND announce HELLO EVERYONE')
    vil_msg = await client.recv()
    assert vil_msg == 'COMMAND OK'
    snoozy_msg = await snoozy_client.recv()
    assert snoozy_msg == "The very air around you seems to shake as vilmibm's booming voice says HELLO EVERYONE"
    await snoozy_client.close()
    await client.close()

@pytest.mark.asyncio
async def test_witch_script(event_loop, mock_logger, client):
    await setup_user(client, 'vilmibm', god=True)
    vil = UserAccount.get(UserAccount.username=='vilmibm')
    horse_script = Script.create(
        name='horse',
        author=vil)
    script_rev = ScriptRevision.create(
        script=horse_script,
        code='''
            (witch "horse" by "vilmibm"
              (has {"num-pets" 0})
              (hears "pet"
                (set-data "num-pets" (+ 1 (get-data "num-pets")))
                  (if (= 0 (% (get-data "num-pets") 5))
                    (says "neigh neigh neigh i am horse"))))''')
    snoozy = GameObject.create(
        author=vil,
        name='snoozy',
        shortname='snoozy',
        script_revision=script_rev)
    foyer = GameObject.get(GameObject.shortname=='foyer')
    GameWorld.put_into(foyer, snoozy)
    for _ in range(0, 4):
        await client.send('COMMAND pet')
        msg = await client.recv()
        assert msg == 'COMMAND OK'
    await client.send('COMMAND pet')
    await client.recv()
    msg = await client.recv()
    assert msg == 'snoozy says, "neigh neigh neigh i am horse"'
    await client.close()


# TODO lookup if i can do a websocket client as context manager, i think i can?

@pytest.mark.asyncio
async def test_whisper_no_args(event_loop, mock_logger, client):
    await setup_user(client, 'vilmibm')
    await client.send('COMMAND whisper')
    msg = await client.recv()
    assert msg == 'ERROR: try /whisper another_username some cool message'
    await client.close()

@pytest.mark.asyncio
async def test_whisper_no_msg(event_loop, mock_logger, client):
    await setup_user(client, 'vilmibm')
    await client.send('COMMAND whisper snoozy')
    msg = await client.recv()
    assert msg == 'ERROR: try /whisper another_username some cool message'
    await client.close()

@pytest.mark.asyncio
async def test_whisper_bad_target(event_loop, mock_logger, client):
    await setup_user(client, 'vilmibm')
    await client.send('COMMAND whisper snoozy hey what are the haps')
    msg = await client.recv()
    assert msg == 'ERROR: there is nothing named snoozy near you'
    await client.close()

@pytest.mark.asyncio
async def test_whisper(event_loop, mock_logger, client):
    await setup_user(client, 'vilmibm')
    snoozy_client = await websockets.connect('ws://localhost:5555', loop=event_loop)
    await setup_user(snoozy_client, 'snoozy')
    await client.send('COMMAND whisper snoozy hey here is a conspiracy')
    vil_msg = await client.recv()
    assert vil_msg == 'COMMAND OK'
    snoozy_msg = await snoozy_client.recv()
    assert snoozy_msg == "vilmibm whispers so only you can hear: hey here is a conspiracy"
    await snoozy_client.close()
    await client.close()


@pytest.mark.asyncio
async def test_look(event_loop, mock_logger, client):
    await setup_user(client, 'vilmibm')
    vil = UserAccount.get(UserAccount.username=='vilmibm')
    snoozy_client = await websockets.connect('ws://localhost:5555', loop=event_loop)
    await setup_user(snoozy_client, 'snoozy')
    cigar = GameObject.create(
        author=vil,
        name='cigar',
        shortname='cigar',
        description='a fancy cigar ready for lighting')
    phone = GameObject.create(
        author=vil,
        name='smartphone',
        shortname='smartphone')
    app = GameObject.create(
        author=vil,
        name='Kwam',
        shortname='kwam',
        description='A smartphone application for KWAM')
    foyer = GameObject.get(GameObject.name=='Foyer')
    GameWorld.put_into(foyer, phone)
    GameWorld.put_into(foyer, cigar)
    GameWorld.put_into(phone, app)

    await client.send('COMMAND look')
    # we expect 4 messages: snoozy, room, phone, cigar. we *shouldn't* see app.
    msgs = set()
    for _ in range(0, 4):
        msgs.add(await client.recv())
    assert {'You are in the Foyer, {}'.format(foyer.description),
            'You see a cigar, a fancy cigar ready for lighting',
            'You see a smartphone',
            'You see snoozy, a gaseous cloud'}
    await client.close()
    await snoozy_client.close()


@pytest.mark.asyncio
async def test_client_state(event_loop, mock_logger, client):
    await client.send('REGISTER vilmibm:foobarbazquux')
    await client.recv()

    vilmibm = UserAccount.get(UserAccount.username=='vilmibm')
    god = UserAccount.get(UserAccount.username=='god')

    room = GameObject.create(
        name='ten forward',
        shortname='ten-forward',
        description='the bar lounge of the starship enterprise.',
        author=god)
    quadchess = GameObject.create(
        shortname='quadchess',
        name='quadchess',
        description='a chess game with four decks',
        author=god)
    chess_piece = GameObject.create(
        name='chess piece',
        shortname='chess-piece',
        description='a chess piece. Looks like a bishop.',
        author=god)
    drink = GameObject.create(
        name='weird drink',
        shortname='weird-drink',
        description='an in-house invention of Guinan. It is purple and fizzes ominously.',
        author=god)
    tricorder = GameObject.create(
        name='tricorder',
        shortname='tricorder',
        description='looks like someone left their tricorder here.',
        author=god)
    medical_app = GameObject.create(
        name='medical program',
        shortname='medical-program',
        description='you can use this to scan or call up data about a patient.',
        author=god)
    patient_file = GameObject.create(
        name='patient file',
        shortname='patient-file',
        description='a scan of Lt Barclay',
        author=god)
    phase_analyzer_app = GameObject.create(
        name='phase analyzer program',
        shortname='phase-analyzer-program',
        description='you can use this to scan for phase shift anomalies',
        author=god)
    music_app = GameObject.create(
        name='media app',
        shortname='media-app',
        description='this program turns your tricorder into a jukebox',
        author=god)
    klingon_opera = GameObject.create(
        shortname='klingon-opera-music',
        name='klingon opera music',
        description='a recording of a klingon opera',
        author=god)
    GameWorld.put_into(room, quadchess)
    GameWorld.put_into(quadchess, chess_piece)
    GameWorld.put_into(room, drink)
    GameWorld.put_into(vilmibm.player_obj, tricorder)
    GameWorld.put_into(tricorder, medical_app)
    GameWorld.put_into(medical_app, patient_file)
    GameWorld.put_into(tricorder, phase_analyzer_app)
    GameWorld.put_into(tricorder, music_app)
    GameWorld.put_into(music_app, klingon_opera)

    await client.send('LOGIN vilmibm:foobarbazquux')
    await client.recv()
    await client.recv()

    GameWorld.put_into(room, vilmibm.player_obj)

    data_msg = await client.recv()
    assert data_msg.startswith('STATE ')
    payload = json.loads(data_msg[len('STATE '):])
    assert payload == {
        'motd': 'welcome to tildemush',
        'user': {
            'username': 'vilmibm',
            'display_name': 'vilmibm',
            'description': 'a gaseous cloud'
        },
        'room': {
            'name': 'ten forward',
            'description': 'the bar lounge of the starship enterprise.',
            'contains': [
                {'name': 'quadchess',
                 'description': 'a chess game with four decks'},
                {'name': 'weird drink',
                 'description': 'an in-house invention of Guinan. It is purple and fizzes ominously.'},
            ],
            'exits': {
                'north': None,
                'south': None,
                'east': None,
                'west': None,
                'above': None,
                'below': None,
            }
        },
        'inventory': [
            {'name':'tricorder',
             'description': 'looks like someone left their tricorder here.',
             'contains': [
                 {'name': 'medical program',
                  'description': 'you can use this to scan or call up data about a patient.',
                  'contains': [{'name': 'patient file',
                                'description': 'a scan of Lt Barclay',
                                'contains': []}]},
                 {'name': 'phase analyzer program',
                  'description': 'you can use this to scan for phase shift anomalies',
                  'contains': []},
                 {'name': 'media app',
                  'description': 'this program turns your tricorder into a jukebox',
                  'contains': [
                      {'name': 'klingon opera music',
                       'description': 'a recording of a klingon opera',
                       'contains': []}]}]}
        ],
        'scripts': []
    }
    await client.close()

@pytest.mark.asyncio
async def test_create_item(event_loop, mock_logger, client):
    await setup_user(client, 'vilmibm')
    vil = UserAccount.get(UserAccount.username=='vilmibm')
    await client.send('COMMAND create item "A fresh cigar" An untouched black and mild with a wood tip')
    msg = await client.recv()
    assert msg == 'COMMAND OK'

    msg = await client.recv()
    assert msg.startswith('STATE')

    msg = await client.recv()
    assert msg == 'You breathed light into a whole new item. Its true name is a-fresh-cigar-vilmibm'

    # create a dupe
    await client.send('COMMAND create item "A fresh cigar" An untouched black and mild with a wood tip')
    msg = await client.recv()
    assert msg == 'COMMAND OK'

    msg = await client.recv()
    assert msg.startswith('STATE')

    msg = await client.recv()
    assert msg == 'You breathed light into a whole new item. Its true name is a-fresh-cigar-vilmibm-3'

    cigar = GameObject.get_or_none(GameObject.shortname=='a-fresh-cigar-vilmibm')
    dupe = GameObject.get_or_none(GameObject.shortname=='a-fresh-cigar-vilmibm-3')

    assert cigar is not None
    assert dupe is not None

    assert 'A fresh cigar' == cigar.get_data('name')
    assert 'A fresh cigar' == dupe.get_data('name')
    assert 'An untouched black and mild with a wood tip' == cigar.get_data('description')
    assert 'An untouched black and mild with a wood tip' == dupe.get_data('description')

    await client.close()
