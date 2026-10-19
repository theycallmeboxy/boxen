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


class TooBig(Refused):
    pass


def size_text(n):
    return "%d MiB" % (n >> 20) if n >= 1 << 20 and not n % (1 << 20) else "%d KiB" % (n >> 10)


def _read_fd(fd, limit):
    """All of the regular file open on fd, as bytes; TooBig when it's over
    limit bytes (never a part of it)."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise Refused("not a regular file")
    if st.st_size > limit:
        raise TooBig("over %s" % size_text(limit))
    chunks, n = [], 0
    while True:
        b = os.read(fd, limit + 1 - n)
        if not b:
            break
        chunks.append(b)
        n += len(b)
        if n > limit:          # it grew since the fstat
            raise TooBig("over %s" % size_text(limit))
    return b"".join(chunks)


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
    """read_bytes_under, as text (invalid UTF-8 replaced): for showing a
    file, never for writing it back."""
    data = read_bytes_under(base, rel, limit)
    return data.decode("utf-8", "replace") if data is not None else None


def read_bytes_under(base, rel, limit=65536):
    """The bytes of the file base/rel[0]/.../rel[-1], with nothing under
    base followed if it's a link. None when it (or a folder on the way)
    doesn't exist; Refused for a link or anything but a plain file, TooBig
    (a Refused) when it's over limit bytes."""
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
        except TooBig as e:
            raise TooBig("%s is %s" % (os.path.join(*rel), e)) from None
        except Refused:
            raise Refused("%s isn't a plain file" % os.path.join(*rel)) from None
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def state_under(base, rel, limit=None):
    """For the Files tab: "file", "missing" (it, or a folder on the way,
    isn't there), or why it would be refused (over limit bytes, too)."""
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
    if limit is not None and st.st_size > limit:
        return "%s is over %s" % (os.path.join(*rel), size_text(limit))
    return "file"
