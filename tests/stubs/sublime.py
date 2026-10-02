""" Minimal sublime API stub for offline tests """


class Region:
    def __init__(self, a, b=None):
        self.a = a
        self.b = a if b is None else b

    def begin(self):
        return min(self.a, self.b)

    def end(self):
        return max(self.a, self.b)

    def empty(self):
        return self.a == self.b


OP_EQUAL = 0
OP_NOT_EQUAL = 1

_settings = {}
_clipboard = [""]
_timeouts = []


class Settings(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def set(self, key, value):
        self[key] = value

    def erase(self, key):
        self.pop(key, None)

    def has(self, key):
        return key in self

    def add_on_change(self, tag, callback):
        pass

    def clear_on_change(self, tag):
        pass


def load_settings(name):
    return _settings.setdefault(name, Settings())


_saved = []


def save_settings(name):
    _saved.append(name)


def set_timeout(fn, delay=0):
    _timeouts.append(fn)


def set_timeout_async(fn, delay=0):
    _timeouts.append(fn)


def run_timeouts():
    while _timeouts:
        _timeouts.pop(0)()


def set_clipboard(text):
    _clipboard[0] = text


def get_clipboard():
    return _clipboard[0]


def packages_path():
    import tempfile
    return tempfile.gettempdir()


def expand_variables(value, variables):
    return value


def version():
    return "4200"


def active_window():
    return None
