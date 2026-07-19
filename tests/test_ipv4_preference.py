"""Tests for the IPv4-first address ordering.

Why the ordering exists: Python's `socket.create_connection` walks resolved addresses
**serially, in resolver order**, and the resolver hands back AAAA (IPv6) first. On a
network that advertises IPv6 and then black-holes it, each dead AAAA burns a full TCP
connect timeout (~28s observed) before the next address is tried — so the A record that
answers in 0.22s is never reached inside a short runner budget.

curl and browsers never see this: Happy Eyeballs (RFC 8305) races the families. The SDK
takes the deterministic half of that idea — reorder, don't race — so the working address
is simply tried first.

The tests below pin the two properties that make a reorder *safe* rather than reckless:
IPv4 comes first, and nothing else about the resolution changes (no address is dropped,
so an IPv6-only network still connects; and order within a family is preserved, so the
resolver's own preference among equals still stands).
"""

from __future__ import annotations

import socket

import wnd_check


def _info(family: int, ip: str) -> tuple:
    """A getaddrinfo 5-tuple, trimmed to the parts the ordering cares about."""
    return (family, socket.SOCK_STREAM, 6, "", (ip, 443))


# A realistic resolution: four dead AAAAs ahead of the A records that actually answer.
V6 = [_info(socket.AF_INET6, f"2001:4860:4802:3{n}::223") for n in (2, 6, 4, 8)]
V4 = [_info(socket.AF_INET, ip) for ip in ("216.239.36.223", "216.239.34.223")]


def _families(infos: list[tuple]) -> list[int]:
    return [info[0] for info in infos]


def test_ipv4_comes_first() -> None:
    ordered = wnd_check._prefer_ipv4(lambda *a, **kw: V6 + V4)()
    assert _families(ordered) == [socket.AF_INET] * 2 + [socket.AF_INET6] * 4


def test_no_address_is_dropped() -> None:
    """A reorder, not a filter — an IPv6-only network must still connect."""
    ordered = wnd_check._prefer_ipv4(lambda *a, **kw: V6 + V4)()
    assert sorted(ordered) == sorted(V6 + V4)


def test_ipv6_only_resolution_survives() -> None:
    ordered = wnd_check._prefer_ipv4(lambda *a, **kw: V6)()
    assert ordered == V6


def test_order_within_a_family_is_preserved() -> None:
    """The resolver's own preference among equals is not ours to reshuffle."""
    ordered = wnd_check._prefer_ipv4(lambda *a, **kw: V6 + V4)()
    assert [i[4][0] for i in ordered if i[0] is socket.AF_INET] == [i[4][0] for i in V4]
    assert [i[4][0] for i in ordered if i[0] is socket.AF_INET6] == [i[4][0] for i in V6]


def test_already_ipv4_first_is_untouched() -> None:
    ordered = wnd_check._prefer_ipv4(lambda *a, **kw: V4 + V6)()
    assert ordered == V4 + V6


def test_arguments_are_passed_through() -> None:
    """We wrap the resolver; we don't get to change what's asked of it."""
    seen: dict = {}

    def fake(*args: object, **kwargs: object) -> list:
        seen["args"], seen["kwargs"] = args, kwargs
        return V4

    wnd_check._prefer_ipv4(fake)("example.com", 443, type=socket.SOCK_STREAM)
    assert seen["args"] == ("example.com", 443)
    assert seen["kwargs"] == {"type": socket.SOCK_STREAM}


def test_importing_the_sdk_installs_the_preference() -> None:
    """The delivery mechanism: every check inherits this simply by importing wnd_check."""
    assert getattr(socket.getaddrinfo, "__wnd_ipv4_first__", False)


def test_install_is_idempotent() -> None:
    """Re-importing (or a check calling it again) must not double-wrap the resolver."""
    installed = socket.getaddrinfo
    wnd_check.install_ipv4_preference()
    wnd_check.install_ipv4_preference()
    assert socket.getaddrinfo is installed
