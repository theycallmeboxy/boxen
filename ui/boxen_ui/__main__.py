import sys

if sys.argv[1:] == ["--check"]:
    # install-venv.sh: the packages import and the app loads; no terminal needed.
    import textual

    from . import app
    print("boxen ui ok (textual %s, %s)" % (textual.__version__, app.BoxenApp.__name__))
    sys.exit(0)

from .app import main  # noqa: E402

main()
