"""Regression: the profile card's real action labels on LinkedIn's layout as of 2026-11-12 (captured from live profiles).

Connect and Message are links, Follow and More are buttons, and the card also holds noise (notification bell, Contact info,
a Premium upsell). classify_connection must still pick the right state from that list."""
from harvey.integrations import linkedin_selectors as sel

NOISE = ["Contact info", "Save this member as a prospect with Premium"]


def test_second_degree_with_connect_link_is_connectable():
    labels = ["Manage notifications about Sam Sample", *NOISE, "Invite Sam Sample to connect", "Message", "More"]
    assert sel.classify_connection(labels, sel.parse_degree("Sam Sample · 2nd\nCo-Founder")) == "connectable"


def test_third_degree_with_message_and_follow_defers_to_the_more_menu():
    labels = ["Manage notifications about Tim Joyce", *NOISE, "Message", "Follow Tim Joyce", "More"]
    assert sel.classify_connection(labels, sel.parse_degree("Tim Joyce · 3rd\nCEO")) == "more"


def test_follow_only_primary_on_a_second_degree_profile_defers_to_the_more_menu():
    labels = ["Follow Paul Winters", "Message", *NOISE, "More"]
    assert sel.classify_connection(labels, sel.parse_degree("Paul Winters · 2nd\nCTO")) == "more"


def test_first_degree_message_means_connected_and_pending_wins():
    assert sel.classify_connection(["Message", "More"], "1st") == "connected"
    assert sel.classify_connection(["Pending", "Message", "More"], "2nd") == "pending"


def test_profile_heading_is_not_an_h1():
    assert "h1" not in sel.PROFILE_HEADING
