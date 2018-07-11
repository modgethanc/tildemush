import io
import os

import hy

from .config import get_db
from .errors import ClientException, WitchException

WITCH_HEADER = '(require [tmserver.witch_header [*]])'

# Note an awful thing here; since we call .format on the script templates, we
# have to escape the WITCH macro's {}. {{}} is not the Hy that we want, but we
# need it in the templates.
# TODO consider using shortname instead of name for the string passed to (witch)
SCRIPT_TEMPLATES = {
    'item': '''
    (witch "{name}"
      (has {{"name" "{name}"
            "description" "{description}"}}))
    ''',
    'player': '''
    (witch "{name}"
      (has {{"name" "{name}"
            "description" "{description}"}}))
    ''',
    'room': '''
    (witch "{name}"
      (has {{"name" "{name}"
            "description" "{description}"}}))
    ''',
    'exit': '''
    (witch "{name}"
      (has {{"name" "{name}"
            "description" "{description}"
            "target" "{target_room_name}"}})
      (hears "touch"
        (tell-sender "move" (get-data "target"))))
    ''',
    'portkey': '''
    (witch "{name}"
      (has {{"name" "{name}"
            "description" "{description}"
            "target" "{target_room_name}"}})
      (hears "touch"
        (tell-sender "move" (get-data "target"))))
    '''}

class ScriptEngine:
    CONTAIN_TYPES = {'acquired', 'entered', 'lost', 'freed'}
    def __init__(self):
        self.handlers = {'debug': self._debug_handler,
                         'contain': self._contain_handler,
                         'say': self._say_handler,
                         'announce': self._announce_handler,
                         'whisper': self._whisper_handler}

    @staticmethod
    def noop(*args, **kwargs):
        pass

    def _ensure_game_world(self, game_world):
        if not hasattr(self, 'game_world'):
            self.game_world = game_world

    def _debug_handler(self, receiver, sender, action_args):
        return '{} <- {} with {}'.format(receiver, sender, action_args)

    def _contain_handler(self, receiver, sender, action_args):
        contain_type = action_args
        if contain_type not in self.CONTAIN_TYPES:
            raise ClientException('Bad container relation: {}'.format(contain_type))
        if receiver.user_account:
            self.game_world.send_client_update(receiver.user_account)
            # TODO we actually want the client to show messages about these
            # events, i think. we can implement that once we actually implement
            # movement and inventory commands. until then we just care that the
            # client_state payload is sent.

    def _announce_handler(self, receiver, sender, action_args):
        if receiver.user_account:
            msg = "The very air around you seems to shake as {}'s booming voice says {}".format(
                sender.name, action_args)
            self.game_world.user_hears(receiver, sender, msg)

    def _say_handler(self, receiver, sender, action_args):
        if receiver.user_account:
            msg = '{} says, \"{}\"'.format(sender.name, action_args)
            self.game_world.user_hears(receiver, sender, msg)

    def _whisper_handler(self, receiver, sender, action_args):
        if receiver.user_account:
            msg = '{} whispers so only you can hear: {}'.format(sender.name, action_args)
            self.game_world.user_hears(receiver, sender, msg)

    def add_handler(self, action, fn):
        self.handlers[action] = fn

    def handler(self, game_world, action):
        self._ensure_game_world(game_world)
        return self.handlers.get(action, self.noop)

class ScriptedObjectMixin:
    """This database-less class implements the runtime behavior of a tildemush
    object. The GameObject represents all of the stuff that's persisted about a
    game object in the DB.


    THIS CLASS DOES NOT STAND-ALONE. It has hard depdencies on GameObject, and
    is split out to enhance readbility. In general, when working on script
    handling, you aren't interested in a GameObject's persisted data and
    vice-versa.
    """

    @classmethod
    def get_template(cls, obj_type):
        return SCRIPT_TEMPLATES[obj_type]

    @property
    def engine(self):
        # TODO sadness, a circular dependency got introduced here
        # as it is this module is a hack to just save on lines in models.py.
        # models.py should probably just be refactored into a hierarchy of
        # smaller files; until then i'm going to be disgusting and add a
        # .latest_script_rev method to GameObject
        if not hasattr(self, '_engine'):
            self.init_scripting()
        else:
            with get_db().atomic():
                # TODO this looks stupid and weird. Consider some kind of
                # 'live_script_rev' that is probably just an alias for
                # GameObject.script_revision; alternatively, change
                # latest_script_rev to like get_latested_script_rev() or
                # something.
                current_rev = self.script_revision
                latest_rev = self.latest_script_rev
                if latest_rev.id != current_rev.id:
                    try:
                        self.script_revision = latest_rev
                        self.init_scripting()
                    except WitchException as e:
                        self.script_revision = current_rev
                        # TODO log
                    else:
                        self.save()
        return self._engine

    def init_scripting(self):
        if self.script_revision is None:
            self._engine = ScriptEngine()
        else:
            try:
                self._engine = self._execute_script(self.script_revision.code)
            except Exception as e:
                raise WitchException(
                    ';_; There is a problem with your witch script: {}'.format(e))

    def handle_action(self, game_world, sender_obj, action, action_args):
        self._ensure_world(game_world)
        # TODO there are *horrifying* race conditions going on here if set_data
        # and get_data are used in separate transactions. Call handler inside
        # of a transaction:
        return self.engine.handler(game_world, action)(self, sender_obj, action_args)

    # say, set_data, get_data, and tell_sender are part of the WITCH scripting
    # API. that should probably be explicit somehow?

    def say(self, message):
        self.game_world.dispatch_action(self, 'say', message)

    # TODO I may want to forbid getting/setting things not originally declared
    # via ensure_data. This might help newer programmers catch typos in WITCH
    # scripts. For now, eh.
    # lol this would have saved me some debugging earlier when i mixed up - and _
    def set_data(self, key, value):
        self.data[key] = value
        self.save()

    def get_data(self, key, default=None):
        return self.get_by_id(self.id).data.get(key, default)

    def tell_sender(self, sender_obj, action, args):
        self.game_world.dispatch_action(sender_obj, action, args)

    def _execute_script(self, witch_code):
        """Given a pile of script revision code, this function prepends the
        (witch) macro definition and then reads and evals the combined code."""
        script_text = self.script_revision.code
        with_header = '{}\n{}'.format(WITCH_HEADER, script_text)
        buff = io.StringIO(with_header)
        stop = False
        result = None
        while not stop:
            try:
                tree = hy.read(buff)
                result = hy.eval(tree,
                                 namespace={'ScriptEngine': ScriptEngine,
                                            'ensure_obj_data': lambda data: self._ensure_data(data)})
            except EOFError:
                stop = True
        return result

    def _ensure_data(self, data_mapping):
        """Given the default values for some gameobject's script, initialize
        this object's data column to those defaults. Saves the instance."""
        if data_mapping == {}:
            return

        for k,v in data_mapping.items():
            if k not in self.data:
                self.data[k] = v

        self.save()

    def _ensure_world(self, game_world):
        if not hasattr(self, 'game_world'):
            self.game_world = game_world
