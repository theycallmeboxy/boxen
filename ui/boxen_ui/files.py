"""Reading a project's files without following links.

A project's folder (~/src/<name>, .boxen/ in it included) is written by its
agent, so a file there can be a symlink planted to make the VM side read
something else (the GitHub App's key, say). Like bin/boxen env_read, these
open every path component that could be a link with O_NOFOLLOW and check
that what was opened is a regular file, then read from that descriptor."""
import errno
import os
import stat


class Refused(Exception):
    pass


_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _read_fd(fd, limit):
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise Refused("not a regular file")
    return os.read(fd, limit).decode("utf-8", "replace")


def open_dir(base, parts):
    """A descriptor for base/parts[0]/.../parts[-1], every one of parts
    opened without following links (base itself is boxen's: ~/src/<name>
    or projects/<name>, opened without following a link either). Raises
    FileNotFoundError, or Refused for a link or a non-folder."""
    try:
        fd = os.open(base, _DIR)
    except FileNotFoundError:
        raise
    except OSError as e:
        raise Refused("%s is a symlink or not a folder" % base if e.errno in (errno.ELOOP, errno.ENOTDIR)
                      else (e.strerror or str(e))) from None
    shown = ""
    for part in parts:
        shown = os.path.join(shown, part)
        try:
            nfd = os.open(part, _DIR, dir_fd=fd)
        except FileNotFoundError:
            os.close(fd)
            raise
        except OSError as e:
            os.close(fd)
            raise Refused("%s/ is a symlink or not a folder" % shown if e.errno in (errno.ELOOP, errno.ENOTDIR)
                          else (e.strerror or str(e))) from None
        os.close(fd)
        fd = nfd
    return fd


def read_under(base, rel, limit=65536):
    """The file base/rel[0]/.../rel[-1], up to limit bytes, with nothing
    under base followed if it's a link. None when it (or a folder on the
    way) doesn't exist; Refused for a link or anything but a plain file."""
    try:
        dfd = open_dir(base, rel[:-1])
    except FileNotFoundError:
        return None
    try:
        try:
            fd = os.open(rel[-1], _FLAGS, dir_fd=dfd)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise Refused("%s is a symlink" % os.path.join(*rel) if e.errno == errno.ELOOP
                          else (e.strerror or str(e))) from None
        try:
            return _read_fd(fd, limit)
        except Refused:
            raise Refused("%s isn't a plain file" % os.path.join(*rel)) from None
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def state_under(base, rel):
    """For the Files tab: "file", "missing" (it, or a folder on the way,
    isn't there), or why it would be refused."""
    try:
        dfd = open_dir(base, rel[:-1])
    except FileNotFoundError:
        return "missing"
    except Refused as e:
        return str(e)
    try:
        st = os.stat(rel[-1], dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except OSError as e:
        return e.strerror or str(e)
    finally:
        os.close(dfd)
    if stat.S_ISLNK(st.st_mode):
        return "%s is a symlink" % os.path.join(*rel)
    if not stat.S_ISREG(st.st_mode):
        return "%s isn't a plain file" % os.path.join(*rel)
    return "file"
