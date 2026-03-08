import re
import json
import jinja2
import hashlib

from pathlib import Path

try:
    import fcntl
except ImportError:
    import winfcntl as fcntl

env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(Path(__file__).parent)),
    autoescape=jinja2.select_autoescape(
        disabled_extensions=("py",), default_for_string=False
    ),
    trim_blocks=True,
    lstrip_blocks=True,
)


def env_filter(name=None):
    return lambda func: env.filters.setdefault(name or func.__name__, func)


@env_filter()
def cls_name(name):
    delims = list(map(re.escape, (" ", "_")))
    parts = re.split("|".join(delims), name)
    return "".join(x.capitalize() for x in parts)


class Generator(object):
    _base_class_name = "BaseError"
    _base_class_module = "apiserver.apierrors.base"

    def __init__(self, path, format_pep8=True, use_md5=True):
        self._use_md5 = use_md5
        self._format_pep8 = format_pep8
        self._path = Path(path)
        self._path.mkdir(parents=True, exist_ok=True)

    def _make_init_file(self, path):
        (self._path / path / "__init__.py").write_bytes(b"")

    def _do_render(self, file, template, context):
        with file.open("w") as f:
            result = template.render(
                base_class_name=self._base_class_name,
                base_class_module=self._base_class_module,
                **context
            )
            if self._format_pep8:
                import autopep8

                result = autopep8.fix_code(
                    result,
                    options={"aggressive": 1, "verbose": 0, "max_line_length": 120},
                )
            f.write(result)

    def _make_section(self, name, code, subcodes):
        self._do_render(
            file=(self._path / name).with_suffix(".py"),
            template=env.get_template("templates/section.jinja2"),
            context=dict(code=code, subcodes=list(subcodes.items())),
        )

    def _make_init(self, sections):
        self._do_render(
            file=(self._path / "__init__.py"),
            template=env.get_template("templates/init.jinja2"),
            context=dict(sections=sections),
        )

    def _key_to_str(self, data):
        if isinstance(data, dict):
            return {str(k): self._key_to_str(v) for k, v in data.items()}
        return data

    def _calc_digest(self, data):
        data = json.dumps(self._key_to_str(data), sort_keys=True)
        return hashlib.md5(data.encode("utf8")).hexdigest()

    def make_errors(self, errors):
        digest = None
        digest_file = self._path / "digest.md5"
        lock_file = self._path / ".generation.lock"

        # Use file locking to prevent race condition when multiple workers
        # try to generate files simultaneously during gunicorn startup
        with lock_file.open("a") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                if self._use_md5:
                    digest = self._calc_digest(errors)
                    if digest_file.is_file():
                        if digest_file.read_text() == digest:
                            return

                self._make_init(errors)
                for (code, section_name), subcodes in errors.items():
                    self._make_section(section_name, int(code), subcodes)

                if self._use_md5:
                    digest_file.write_text(digest)

            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)