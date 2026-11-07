"""The recording volume: telling what it is formatted as, and saying so
when that is FAT32 or exFAT."""

from __future__ import annotations

from recorder import session as session_mod
from recorder.format import read_meta
from recorder.session import SessionManager
from recorder.writer import fs_type_from_mounts, fs_warning

from .helpers import settings

MOUNTS = r"""/dev/nvme0n1p1 / ext4 rw,relatime 0 0
proc /proc proc rw 0 0
/dev/mmcblk1p1 /media/sd\040card vfat rw 0 0
/dev/sda1 /media/usb exfat rw 0 0
/dev/nvme0n1p2 /data ext4 rw 0 0
"""


def test_finds_the_mount_a_path_is_on():
    assert fs_type_from_mounts("/media/sd card/rec", MOUNTS) == "vfat"
    assert fs_type_from_mounts("/media/usb/rec/x", MOUNTS) == "exfat"
    assert fs_type_from_mounts("/data", MOUNTS) == "ext4"
    assert fs_type_from_mounts("/home/jetson/rec", MOUNTS) == "ext4"   # root
    assert fs_type_from_mounts("/database", MOUNTS) == "ext4"          # not /data


def test_warns_about_fat_and_exfat_only():
    assert "4 GB" in fs_warning("vfat") and "ext4" in fs_warning("vfat")
    assert "journal" in fs_warning("exfat") and "4 GB" not in fs_warning("exfat")
    assert fs_warning("fuseblk") is not None
    for ok in ("ext4", "xfs", "btrfs", "ntfs", None):
        assert fs_warning(ok) is None


def test_a_fat_volume_shows_in_status_meta_and_log(tmp_path, monkeypatch, capsys):
    fat = {"dir": str(tmp_path), "fs": "vfat", "warning": fs_warning("vfat")}
    monkeypatch.setattr(session_mod, "storage_info", lambda _p: fat)
    m = SessionManager(settings(tmp_path))
    assert m.status()["storage"]["fs"] == "vfat"
    name = m.start()["session"]
    m.stop()
    assert read_meta(tmp_path / name)["storage"]["fs"] == "vfat"
    assert "[storage]" in capsys.readouterr().err
