"""Reading a project's files without following links.

A project's env/ folder (and its code folder) can be written from inside
its container, so a file there can be a symlink planted to make the VM side
read something else (the GitHub App's key, say). Like bin/boxen env_read,
these open every path component that could be a link with O_NOFOLLOW and
check what was opened is a regular file, then read from that descriptor."""
import os
import stat


class Refused(Exception):
    pass


_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _read_fd(fd, limit, tail):
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise Refused("not a regular file")
    if tail and st.st_size > limit:
        os.lseek(fd, st.st_size - limit, os.SEEK_SET)
    data = os.read(fd, limit)
    return data.decode("utf-8", "replace")


def read_file(path, limit=65536, tail=False):
    """The file at path (the last component not followed), up to limit
    bytes (the last limit bytes with tail). None when it doesn't exist;
    Refused when it's a link or not a regular file."""
    try:
        fd = os.open(path, _FLAGS)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == 40:          # ELOOP: a symlink
            raise Refused("is a symlink") from None
        raise Refused(e.strerror or str(e)) from None
    try:
        return _read_fd(fd, limit, tail)
    finally:
        os.close(fd)


def read_env_file(proj_dir, name, limit=65536):
    """env/<name> of a project dir: env/ and the file both opened without
    following links. None when missing; Refused for a link."""
    try:
        dfd = os.open(os.path.join(proj_dir, "env"), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Refused("env/ is a symlink" if e.errno in (40, 20) else (e.strerror or str(e))) from None
    try:
        try:
            fd = os.open(name, _FLAGS, dir_fd=dfd)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise Refused("env/%s is a symlink" % name if e.errno == 40 else (e.strerror or str(e))) from None
        try:
            return _read_fd(fd, limit, False)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def tail_lines(path, n=20):
    text = read_file(path, limit=65536, tail=True)
    if text is None:
        return None
    lines = text.splitlines()
    return lines[-n:]


def plain_file_problem(path, parents=()):
    """Why path shouldn't be opened in an editor, or None: it (or one of
    parents, which must be folders) is a symlink, or it exists and isn't a
    regular file. A missing file is fine (the editor creates it)."""
    for d in parents:
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return "%s is a symlink or not a folder; refusing" % d
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return "%s is a symlink; refusing" % path
    if not stat.S_ISREG(st.st_mode):
        return "%s isn't a regular file" % path
    return None
