"""dmgbuild settings: Finder layout without Apple Events or live Finder changes."""

import os

files = [os.environ["CALLPILOT_DMG_APP"]]
symlinks = {"Applications": "/Applications"}
background = os.environ["CALLPILOT_DMG_BACKGROUND"]
icon_locations = {"CallPilot.app": (175, 205), "Applications": (485, 205)}
window_rect = ((180, 120), (660, 420))
icon_size = 96
text_size = 14
format = "UDZO"
show_toolbar = False
show_status_bar = False
show_tab_view = False
show_pathbar = False
show_sidebar = False
default_view = "icon-view"
include_icon_view_settings = True
include_list_view_settings = False
