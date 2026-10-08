#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
 LOKIMAN 2  -  a retro PS1-style runner (Pepsiman homage, themed on Marvel's Loki)
===============================================================================
 Requirements :  Python 3.8+  and  pygame 2.x      (pip install pygame)
 Run          :  python lokiman2.py            [--stage N] [--scene N] [--windowed]
 No external assets: everything is drawn with coloured geometric shapes and
 pseudo-3D scaling.  The save file (unlocked skins / expert mode) is written to
 ~/.lokiman2_save.json.

 CONTROLS
   LEFT / RIGHT (A / D)      change lane (3 lanes)
   UP (W)                    jump  (low debris, gaps)
   DOWN (S)                  slide (high hazards)
   SPACE  or  UP+DOWN        DASH  (speeds the level up, smashes breakable walls,
                             longer jumps = "dash-jump")
   LSHIFT / B  or DOWN+SPACE BRAKE (screeching halt for cross-traffic)
   P pause     ESC back to menu

 HOW THE LEVEL ENGINE WORKS  (read this before editing LEVEL_DATA!)
 -------------------------------------------------------------------------------
 Every scene is just DATA.  `load_level(stage, scene)` turns a scene description
 into one flat, time-sorted list of spawn tuples:

        (time_in_frames, obstacle_type, lane, speed)

   time_in_frames : scene clock tick (60 = 1 second at normal run speed) at which
                    the object appears on the horizon.  The clock runs faster
                    while dashing and almost stops while braking.
   obstacle_type  : a key of the OB table below ("TAXI", "BLASTER", "TESS" ...)
                    OR an event key starting with "EV_" (see EVENTS).
   lane           : 0 = left, 1 = centre, 2 = right, -1 = whole road width.
                    For "cross" objects (Iron Man, speeders) it is the lane they
                    are aimed at; for EV_ events it is ignored.
   speed          : multiplier of the object's approach speed (1.0 = normal).
                    For EV_ events this number is the DURATION in frames.

 A scene description (see LEVEL_DATA) has two optional ways to produce tuples:

   'spawns'   : a hand written list of tuples - total control (Stage 1 uses it).
                Helpers: tess_run(), zigzag(), hidden_tess(), row().
   'segments' : a list of generator segments, expanded with a seeded RNG, so a
                scene is reproducible.  Each segment is made with seg():

        seg(start, end, pool, gap=(min,max), tess=chance, speed=1.0)

        pool = [("TAXI", 3), ("CAR", 2)]   -> weighted choice of obstacle types
        gap  = frames between two obstacle rows
        tess = probability of a Tesseract trail after a row

        pattern="safe_seq" is used by the boss chases (scene 3): the lane
        numbers in seq=[2,0,1] are the SAFE lanes, the other two lanes are
        filled with `hazard`; `fill=[...]` adds full-width jump/slide items.

 TO ADD CONTENT:
   * New obstacle ........ add a line to OB (kind decides how it is avoided).
   * New row in a scene .. append a tuple to that scene's 'spawns' list.
   * Harder/longer scene . add a seg(...) with a smaller gap, or extend 'end'.
   * New scene/stage ..... add a (stage, scene) key to LEVEL_DATA and (if you
                           want a new theme) a THEMES entry.
   * Quick auto-scenes ... auto_scene() builds N escalating acts from pools.
===============================================================================
"""
import sys
import os
import json
import math
import random
import argparse
from collections import namedtuple

import pygame

# ----------------------------------------------------------------------------
# CONSTANTS
# ----------------------------------------------------------------------------
W, H = 960, 600
FPS = 60
HORIZON = 190                    # y of the vanishing line
BOTTOM = H                       # y where p == 1.0
SCENE_FRAMES = 9000              # 150 s per scene  ->  12 scenes ~ 30 minutes
APPROACH = 150.0                 # frames an object needs from horizon to player
SAVE_PATH = os.path.join(os.path.expanduser("~"), ".lokiman2_save.json")

T_N = 0.916                      # normal cam: perspective parameter of player
P_N = T_N ** 1.8                 # ... -> p = 0.854  (player foot y ~ 540)
T_R = 0.5 ** (1 / 1.8)           # reverse cam: player stands at p = 0.5
P_R = 0.5

STATES = ("TITLE", "MENU", "PLAYING", "STAGE_CLEAR", "GAME_OVER", "VICTORY")

SKINS = {
    # name: (display, primary, secondary, skin tone, horn colour)
    "default": ("MCU Loki (Green & Gold)", (28, 120, 62), (235, 190, 50), (228, 205, 185), (240, 200, 60)),
    "classic": ("Classic Loki (Yellow & Green)", (232, 214, 48), (34, 150, 56), (228, 205, 185), (232, 214, 48)),
    "frost": ("Frost Giant Loki (Blue)", (28, 62, 130), (160, 210, 255), (96, 150, 232), (190, 225, 255)),
}
SKIN_ORDER = ["default", "classic", "frost"]

# ----------------------------------------------------------------------------
# OBSTACLE TABLE
#   kind : how the player may get past it
#     pickup : collect it (Tesseract)
#     jump   : be airborne        gap  : be airborne (deep pit, long window)
#     slide  : be sliding         dash : be dashing (wall is smashed)
#     solid  : change lane (or brake, for cross-traffic)
# ----------------------------------------------------------------------------
Spec = namedtuple("Spec", "kind color wf hf depth style full lat latspd spd label gate")


def O(kind, color, wf=.6, hf=.6, depth=.07, style="box", full=False, lat=None,
      latspd=0.0, spd=1.0, label="", gate=False):
    return Spec(kind, color, wf, hf, depth, style, full, lat, latspd, spd, label, gate)


OB = {
    "TESS": O("pickup", (70, 200, 255), .30, .30, .06, "tess", label="TESSERACT"),
    # ---- Stage 1 : New York
    "TAXI": O("slide", (240, 200, 30), .9, .45, style="oh", label="TAXI"),
    "BLASTER": O("jump", (120, 120, 150), .8, .28, label="BLASTER"),
    "CAR": O("solid", (176, 52, 52), .8, .6, label="CAR"),
    "SPEEDER": O("solid", (90, 120, 215), .7, .35, lat="cross", latspd=.012, spd=1.15, label="SPEEDER"),
    "SHAWARMA": O("dash", (205, 145, 62), 1.0, .95, style="brick", full=True, label="SHAWARMA"),
    "BARRICADE": O("jump", (235, 120, 30), 1.0, .3, full=True, label="BARRICADE"),
    "LIGHT": O("slide", (80, 230, 100), 1.0, .3, style="oh", full=True, label="TRAFFIC LIGHT"),
    "QUINJET": O("slide", (120, 130, 150), .95, .5, style="oh", label="QUINJET"),
    "SCAFFOLD": O("gap", (14, 14, 20), 1.0, .3, .15, "gap", full=True, label="SCAFFOLD GAP"),
    "IRONMAN": O("solid", (205, 40, 40), 2.4, .9, .07, lat="cross", latspd=.03, label="IRON MAN - BRAKE!"),
    "CRATE": O("solid", (160, 120, 70), .7, .6, label="CRATE"),
    "HULK_ARM": O("solid", (88, 170, 62), .9, 1.5, label="HULK ARM"),
    # ---- Stage 2 : Asgard
    "SPEAR": O("jump", (225, 205, 100), .7, .28, label="SPEAR"),
    "PILLAR": O("solid", (232, 192, 52), .5, 1.2, spd=1.3, label="GOLD PILLAR"),
    "BLOCKADE": O("dash", (205, 175, 62), .95, .95, style="brick", label="BLOCKADE"),
    "VOID": O("gap", (70, 25, 140), 1.0, .3, .17, "gap", full=True, label="COSMIC VOID"),
    "ARCH": O("slide", (205, 205, 235), 1.0, .3, style="oh", full=True, label="ARCH"),
    "CASKET": O("solid", (120, 225, 255), .9, .55, label="CASKET OF WINTERS"),
    "CITIZEN": O("solid", (205, 165, 125), .4, .8, lat="cross", latspd=.02, label="CITIZEN"),
    "BEAM": O("solid", (255, 130, 30), .8, 3.0, style="beam", label="BEAM"),
    # ---- Stage 3 : Sakaar
    "SCRAPPER": O("solid", (150, 150, 165), .9, .4, lat="cross", latspd=.016, label="SCRAPPER SHIP"),
    "CANNON": O("jump", (255, 155, 40), .9, .3, label="CANNON FIRE"),
    "BOULDER": O("dash", (140, 118, 92), .9, .9, style="brick", label="SCRAP BOULDER"),
    "BEAST": O("solid", (176, 84, 52), .8, .9, spd=1.5, label="BEAST"),
    "HOVERCART": O("solid", (210, 210, 70), .7, .5, lat="bounce", label="HOVER-CART"),
    "SPIKES": O("gap", (190, 190, 205), 1.0, .3, .12, "gap", full=True, label="SPIKE PIT"),
    "NET": O("jump", (90, 205, 255), 1.0, .35, full=True, label="ELECTRIC NET"),
    "LASER": O("solid", (255, 70, 205), .8, 3.0, style="beam", label="LASER"),
    # ---- Stage 4 : TVA / Void
    "CABINET": O("solid", (135, 145, 158), .6, .95, spd=1.25, label="FILE CABINET"),
    "TRUCK": O("solid", (62, 104, 74), .9, .8, spd=1.1, label="TVA TRUCK"),
    "DOOR": O("solid", (255, 175, 62), .9, 1.1, label="TIME-DOOR", gate=True),
    "MONITOR": O("slide", (84, 225, 205), 1.0, .4, style="oh", full=True, label="MONITOR"),
    "RUINS": O("dash", (155, 105, 72), .95, .95, style="brick", label="PRUNED RUINS"),
    "THUG": O("solid", (96, 64, 116), .45, .85, label="THUG"),
    "COPTER": O("solid", (255, 120, 255), .8, .5, spd=1.2, label="THANOS-COPTER"),
    "PIRATE": O("solid", (112, 72, 42), 1.2, .9, label="PIRATE SHIP"),
}

# Events: key -> (banner text, gameplay effect)
EVENTS = {
    "EV_COLLAR": ("TVA TIME-COLLAR!  CONTROLS REVERSED!", "collar"),
    "EV_HOVER": ("HOVERBOARD!  WEAVE, DON'T JUMP!", "hover"),
    "EV_BARREL": ("SHIELD BARREL-ROLL!  GRAB THE TESSERACTS!", "barrel"),
    "EV_CHARIOT": ("<CHARIOT SEQUENCE>  Chitauri speeders!", None),
    "EV_PILLARS": ("<ROLLING PILLAR SEQUENCE>", None),
    "EV_RELIC": ("<RELIC SEQUENCE>  Casket of Ancient Winters!", None),
    "EV_VALKYRIE": ("<VALKYRIE SHIP SEQUENCE>  Slide and jump!", None),
    "EV_MINUTEMEN": ("<MINUTEMEN TRUCK SEQUENCE>", None),
    "EV_DASH": ("DASH THROUGH!  (SPACE)", None),
}

# ----------------------------------------------------------------------------
# THEMES (colours per scene)
# ----------------------------------------------------------------------------
THEMES = {
    "nyc": dict(sky=((35, 40, 90), (240, 150, 90)), ground=(40, 44, 52), road=(70, 72, 82),
                line=(235, 235, 235), side=(95, 60, 55), sky_line=(25, 25, 50)),
    "tower": dict(sky=((60, 90, 150), (230, 190, 140)), ground=(60, 56, 50), road=(100, 96, 90),
                  line=(255, 210, 60), side=(150, 80, 40), sky_line=(40, 60, 100)),
    "hulk": dict(sky=((30, 70, 50), (170, 220, 120)), ground=(35, 55, 40), road=(80, 85, 80),
                 line=(255, 255, 255), side=(60, 90, 60), sky_line=(20, 50, 40)),
    "asgard": dict(sky=((60, 40, 110), (255, 215, 120)), ground=(120, 95, 40), road=(190, 160, 70),
                   line=(255, 245, 190), side=(230, 200, 90), sky_line=(120, 90, 160)),
    "vault": dict(sky=((20, 20, 60), (90, 120, 190)), ground=(40, 50, 80), road=(100, 120, 160),
                  line=(190, 235, 255), side=(60, 80, 130), sky_line=(30, 40, 90)),
    "destroyer": dict(sky=((40, 20, 20), (210, 110, 40)), ground=(60, 40, 30), road=(110, 90, 70),
                      line=(255, 190, 80), side=(120, 70, 40), sky_line=(60, 30, 25)),
    "sakaar": dict(sky=((120, 50, 130), (255, 150, 60)), ground=(95, 60, 40), road=(150, 110, 80),
                   line=(255, 235, 140), side=(130, 80, 60), sky_line=(100, 40, 90)),
    "arena": dict(sky=((60, 30, 90), (230, 110, 120)), ground=(110, 75, 50), road=(190, 150, 100),
                  line=(255, 255, 255), side=(150, 100, 70), sky_line=(80, 40, 80)),
    "grandmaster": dict(sky=((70, 20, 110), (255, 90, 180)), ground=(60, 30, 80), road=(110, 70, 140),
                        line=(255, 200, 255), side=(150, 70, 170), sky_line=(90, 30, 120)),
    "tva": dict(sky=((190, 130, 60), (250, 220, 150)), ground=(140, 95, 50), road=(205, 160, 100),
                line=(255, 255, 230), side=(170, 120, 70), sky_line=(160, 110, 60)),
    "void": dict(sky=((15, 5, 35), (110, 40, 150)), ground=(25, 15, 45), road=(60, 40, 95),
                 line=(190, 140, 255), side=(50, 30, 80), sky_line=(40, 20, 70)),
    "alioth": dict(sky=((20, 0, 30), (120, 20, 120)), ground=(20, 10, 30), road=(50, 30, 70),
                   line=(255, 120, 255), side=(60, 20, 70), sky_line=(40, 10, 50)),
}


# ----------------------------------------------------------------------------
# LEVEL DATA HELPERS
# ----------------------------------------------------------------------------
def tess_run(f, lane, n, step=16):
    """n Tesseracts in one lane, `step` frames apart."""
    return [(f + i * step, "TESS", lane, 1.0) for i in range(n)]


def zigzag(f, n, step=22):
    """n Tesseracts zig-zagging left-centre-right-centre (barrel-roll bonus)."""
    lanes = (0, 1, 2, 1)
    return [(f + i * step, "TESS", lanes[i % 4], 1.0) for i in range(n)]


def hidden_tess(f, lane, n):
    """A breakable blockade with n Tesseracts hidden right behind it."""
    return [(f, "BLOCKADE", lane, 1.0)] + [(f + 34 + i * 14, "TESS", lane, 1.0) for i in range(n)]


def row(f, *items):
    """row(300, ("CAR",0), ("CAR",2)) -> several obstacles at the same tick."""
    return [(f, n, l, 1.0) for n, l in items]


def seg(a, b, pool=None, gap=(55, 90), tess=.14, speed=1.0, pattern="random", **kw):
    d = dict(start=a, end=b, pool=pool or [], gap=gap, tess=tess, speed=speed, pattern=pattern)
    d.update(kw)
    return d


def expand_segment(rng, s):
    """Turn one segment description into spawn tuples."""
    out = []
    f, end = s["start"], s["end"]
    if s["pattern"] == "safe_seq":
        seq = s["seq"]
        step = s["every"]
        hazard = s["hazard"]
        fill = s.get("fill", [])
        i = 0
        while f < end:
            safe = rng.randrange(3) if seq == "random" else seq[i % len(seq)]
            for l in range(3):
                if l != safe:
                    out.append((f, hazard, l, s["speed"]))
            if rng.random() < s.get("tess", 0.6):
                out.append((f, "TESS", safe, 1.0))
            if fill and i % s.get("fill_every", 2) == 1:
                out.append((f + step // 2, rng.choice(fill), -1, 1.0))
            f += step
            i += 1
        return out
    # --- random pattern
    names = [p[0] for p in s["pool"]]
    weights = [p[1] for p in s["pool"]]
    while f < end:
        name = rng.choices(names, weights)[0]
        sp = OB[name]
        blocked = []
        if sp.gate:
            free = rng.randrange(3)
            for l in range(3):
                if l != free:
                    out.append((f, name, l, s["speed"]))
            blocked = [l for l in range(3) if l != free]
        elif sp.full:
            out.append((f, name, -1, s["speed"]))
        else:
            lane = rng.randrange(3)
            out.append((f, name, lane, s["speed"]))
            blocked = [lane]
            if sp.lat == "cross":
                blocked = []
        if rng.random() < s["tess"]:
            free_lanes = [l for l in range(3) if l not in blocked] or [1]
            tl = rng.choice(free_lanes)
            out += tess_run(f + 26, tl, rng.randint(2, 4))
        f += rng.randint(*s["gap"])
    return out


def acts(start, end, pools, gap_from=(62, 95), gap_to=(44, 68), tess=.14):
    """Split [start,end] into len(pools) acts with shrinking gaps."""
    n = len(pools)
    span = (end - start) / n
    out = []
    for i, pool in enumerate(pools):
        k = i / max(1, n - 1)
        gap = (int(gap_from[0] + (gap_to[0] - gap_from[0]) * k),
               int(gap_from[1] + (gap_to[1] - gap_from[1]) * k))
        out.append(seg(int(start + i * span), int(start + (i + 1) * span), pool, gap=gap, tess=tess))
    return out


def scene(name, cam, theme, boss=None, spawns=None, segments=None, tip=""):
    return dict(name=name, cam=cam, theme=theme, boss=boss, spawns=spawns or [],
                segments=segments or [], tip=tip)


# ----------------------------------------------------------------------------
# LEVEL DATA  -  EDIT / EXPAND ME
# Key = (stage, scene).  Scenes 1-2 use the chase cam, scene 3 the reverse cam.
# Stage 1 is fully hand-authored; stages 2-4 use compact generators.
# ----------------------------------------------------------------------------
LEVEL_DATA = {}

# ===== STAGE 1 : BATTLE OF NEW YORK ===========================================
LEVEL_DATA[(1, 1)] = scene(
    "City Streets", "chase", "nyc",
    tip="Collect Tesseracts!  Slide under taxis (DOWN).  Jump low debris (UP).",
    spawns=(
        tess_run(120, 1, 6)                                   # [02/002] Tesseracts on first road
        + [(330, "BLASTER", 0, 1.0), (390, "BLASTER", 2, 1.0), (450, "CAR", 1, 1.0)]
        + row(560, ("TAXI", 0), ("TAXI", 1))                   # [05/007] crashed taxi cabs
        + tess_run(600, 2, 4)
        + [(720, "BARRICADE", -1, 1.0), (800, "LIGHT", -1, 1.0)]
        + [(1500, "EV_CHARIOT", 0, 1100)]                      # <Chariot Sequence>
        + [(1600 + 70 * i, "SPEEDER", i % 3, 1.0) for i in range(12)]
        + [(2750, "EV_DASH", 0, 400), (2800, "SHAWARMA", -1, 1.0)]   # [10/040] shawarma window
        + tess_run(2860, 1, 6)
        + [(3000, "SHAWARMA", -1, 1.0)]
        + [(3900, "EV_COLLAR", 0, 700)]                        # <Time-Collar Sequence>
        + [(4000 + 80 * i, "CAR" if i % 2 else "BLASTER", (i * 2) % 3, 1.0) for i in range(8)]
        + tess_run(4700, 0, 5)
        + [(8350, "TESS", 1, 1.0)] + tess_run(8400, 1, 8, 20)   # [14/100] final stretch
    ),
    segments=[
        seg(420, 1450, [("TAXI", 3), ("BLASTER", 3), ("CAR", 2), ("LIGHT", 1)], (60, 95)),
        seg(3100, 3850, [("SHAWARMA", 2), ("TAXI", 2), ("CAR", 2), ("BLASTER", 2)], (60, 90)),
        seg(4800, 6300, [("TAXI", 2), ("BLASTER", 3), ("CAR", 3), ("BARRICADE", 1), ("LIGHT", 1), ("SHAWARMA", 1)],
            (52, 85)),
        seg(6400, 8300, [("TAXI", 2), ("BLASTER", 3), ("CAR", 3), ("BARRICADE", 2), ("LIGHT", 2), ("SHAWARMA", 1)],
            (44, 72)),
    ])

LEVEL_DATA[(1, 2)] = scene(
    "Stark Tower Construction", "chase", "tower",
    tip="Dash-jump the scaffolding gaps.  BRAKE (SHIFT) when Iron Man flies past!",
    spawns=(
        tess_run(120, 2, 5)
        + [(400, "QUINJET", 1, 1.0), (470, "QUINJET", 0, 1.0), (540, "QUINJET", 2, 1.0)]   # [06/008]
        + tess_run(600, 1, 5)
        + [(2100 + 55 * i, "SCAFFOLD", -1, 1.0) for i in range(3)]                      # [24/035]
        + [(3600, "IRONMAN", 1, 1.0), (4300, "IRONMAN", 1, 1.0)]                         # [32/084]
        + [(6400, "IRONMAN", 1, 1.0), (7400, "IRONMAN", 1, 1.0)]
        + tess_run(8300, 1, 8, 20)                                                       # [02/100]
    ),
    segments=[
        seg(700, 2000, [("QUINJET", 2), ("CRATE", 3), ("BLASTER", 3), ("CAR", 1)], (58, 92)),
        seg(2400, 3500, [("SCAFFOLD", 3), ("CRATE", 2), ("BLASTER", 2), ("QUINJET", 2)], (60, 95)),
        seg(3800, 4200, [("CRATE", 1), ("BLASTER", 1)], (70, 100)),
        seg(4500, 6300, [("SCAFFOLD", 2), ("CRATE", 3), ("QUINJET", 3), ("BLASTER", 3), ("SHAWARMA", 1)], (48, 80)),
        seg(6600, 7300, [("CRATE", 2), ("QUINJET", 2), ("SCAFFOLD", 2)], (48, 75)),
        seg(7600, 8250, [("SCAFFOLD", 3), ("CRATE", 3), ("QUINJET", 3), ("BLASTER", 3)], (42, 66)),
    ])

LEVEL_DATA[(1, 3)] = scene(
    "The Hulk Chase", "reverse", "hulk", boss="hulk",
    tip="RUN! Stay in the CENTRE lane, jump barricades, slide under traffic lights.",
    segments=[
        seg(300, 3000, pattern="safe_seq", seq=[1], hazard="HULK_ARM", every=90, fill=["BARRICADE", "LIGHT"],
            speed=1.0, tess=.5),
        seg(3000, 6000, pattern="safe_seq", seq=[1], hazard="HULK_ARM", every=76, fill=["BARRICADE", "LIGHT", "BARRICADE"],
            speed=1.1, tess=.5),
        seg(6000, 8300, pattern="safe_seq", seq=[1], hazard="HULK_ARM", every=66, fill=["LIGHT", "BARRICADE"],
            speed=1.2, tess=.5, fill_every=1),
    ])

# ===== STAGE 2 : ASGARD & THE BIFROST =========================================
LEVEL_DATA[(2, 1)] = scene(
    "The Courtyards", "chase", "asgard",
    tip="Jump the spears.  Barrel-roll: swing left/right for 24 Tesseracts!",
    spawns=(
        [(60, "EV_PILLARS", 0, 1500)]
        + [(2500, "EV_BARREL", 0, 640)] + zigzag(2540, 24)                      # [24/042]
        + [(5200, "EV_DASH", 0, 300)]
        + tess_run(8350, 1, 8, 20)
    ),
    segments=(
        [seg(100, 1000, [("SPEAR", 4), ("CRATE", 1)], (55, 85)),
         seg(1000, 2400, [("PILLAR", 4), ("SPEAR", 2)], (50, 80)),
         seg(3300, 5000, [("SPEAR", 4), ("PILLAR", 3), ("BARRICADE", 1)], (50, 80))]
        + [seg(5300, 8300, [("VOID", 3), ("ARCH", 3), ("SPEAR", 1)], (55, 85), tess=.1)]      # [36/100] bridge
    ))

LEVEL_DATA[(2, 2)] = scene(
    "The Vault", "chase", "vault",
    tip="Dash (SPACE) through blockades.  The Casket freezes the lanes ahead!",
    spawns=(
        [(150, "EV_DASH", 0, 300)]
        + hidden_tess(1600, 0, 10)                                              # [10/042]
        + [(3500, "EV_RELIC", 0, 1800)]
        + hidden_tess(5600, 2, 10)
        + tess_run(8350, 1, 8, 20)
    ),
    segments=[
        seg(250, 1500, [("BLOCKADE", 4), ("SPEAR", 2), ("CRATE", 1)], (60, 90)),            # [11/022]
        seg(1900, 3400, [("BLOCKADE", 3), ("CRATE", 2), ("SPEAR", 2)], (55, 85)),
        seg(3600, 5300, [("CASKET", 5), ("CRATE", 1), ("SPEAR", 1)], (50, 80)),
        seg(5900, 7200, [("BLOCKADE", 3), ("CASKET", 2), ("SPEAR", 2)], (48, 75)),
        seg(7200, 8300, [("CITIZEN", 5), ("ARCH", 2), ("CRATE", 1)], (44, 68)),             # [24/100]
    ])

LEVEL_DATA[(2, 3)] = scene(
    "The Destroyer Chase", "reverse", "destroyer", boss="destroyer",
    tip="Memorise the beams:  RIGHT, LEFT, CENTER.",
    segments=[
        seg(300, 2800, pattern="safe_seq", seq=[2, 0, 1], hazard="BEAM", every=95, speed=1.0, tess=.5),
        seg(2800, 5800, pattern="safe_seq", seq=[2, 0, 1], hazard="BEAM", every=78, fill=["BARRICADE"], speed=1.1,
            tess=.5, fill_every=3),
        seg(5800, 8300, pattern="safe_seq", seq=[2, 0, 1, 1, 0, 2], hazard="BEAM", every=68, fill=["ARCH", "BARRICADE"],
            speed=1.2, tess=.5, fill_every=3),
    ])

# ===== STAGE 3 : SAKAAR WASTELANDS ============================================
LEVEL_DATA[(3, 1)] = scene(
    "The Scrapper Heaps", "chase", "sakaar",
    tip="Dodge the hovering Scrapper ships.  Dash through the scrap boulders!",
    spawns=([(2500, "EV_VALKYRIE", 0, 1700), (6000, "EV_DASH", 0, 300)] + tess_run(8350, 1, 8, 20)),
    segments=[
        seg(150, 2400, [("SCRAPPER", 5), ("CRATE", 2), ("BLASTER", 2)], (55, 85)),                  # [22/022]
        seg(2600, 4200, [("CANNON", 4), ("ARCH", 2), ("CRATE", 1)], (48, 78)),
        seg(4300, 6000, [("SCRAPPER", 3), ("CANNON", 2), ("CRATE", 2)], (50, 80)),
        seg(6200, 7700, [("BOULDER", 4), ("SCRAPPER", 2), ("CRATE", 2)], (50, 80)),                 # [12/080]
        seg(7700, 8300, [("BOULDER", 2), ("CRATE", 2), ("CANNON", 2)], (45, 65)),                   # [10/100]
    ])

LEVEL_DATA[(3, 2)] = scene(
    "The Gladiator Pits", "chase", "arena",
    tip="Charging beasts!  Let them pass in the middle and steer clear.",
    spawns=([(120, "BEAST", 1, 1.0), (180, "TESS", 2, 1.0), (240, "TESS", 2, 1.0)]
            + tess_run(8350, 1, 8, 20)),
    segments=[
        seg(300, 2800, [("BEAST", 5), ("CRATE", 1), ("SPIKES", 1)], (48, 78)),                      # [16/016]
        seg(3000, 5800, [("HOVERCART", 5), ("BEAST", 2), ("CRATE", 1)], (50, 80)),                  # [28/073]
        seg(5900, 8300, [("SPIKES", 4), ("BEAST", 3), ("HOVERCART", 2), ("BOULDER", 1)], (46, 72)), # [14/100]
    ])

LEVEL_DATA[(3, 3)] = scene(
    "The Grandmaster Chase", "reverse", "grandmaster", boss="grandmaster",
    tip="Safe lanes: R L R L C R L R C.  Jump the nets!",
    segments=[
        seg(300, 3000, pattern="safe_seq", seq=[2, 0, 2, 0, 1, 2, 0, 2, 1], hazard="LASER", every=88,
            fill=["NET"], speed=1.0, tess=.5, fill_every=2),
        seg(3000, 6000, pattern="safe_seq", seq=[2, 0, 2, 0, 1, 2, 0, 2, 1], hazard="LASER", every=74,
            fill=["NET"], speed=1.1, tess=.5, fill_every=2),
        seg(6000, 8300, pattern="safe_seq", seq=[2, 0, 2, 0, 1, 2, 0, 2, 1], hazard="LASER", every=64,
            fill=["NET", "SPIKES"], speed=1.2, tess=.5, fill_every=2),
    ])

# ===== STAGE 4 : THE TVA & THE VOID AT THE END OF TIME ========================
LEVEL_DATA[(4, 1)] = scene(
    "TVA Headquarters", "chase", "tva",
    tip="Squeeze through the Time-Doors.  Precision: jump, then slide!",
    spawns=(
        [(2800, "EV_MINUTEMEN", 0, 1600), (5000, "EV_COLLAR", 0, 900)]
        + [(7500 + 80 * i, "BARRICADE", -1, 1.0) if i % 2 == 0 else (7500 + 80 * i - 34, "MONITOR", -1, 1.0)
           for i in range(1, 7)]                                                          # [18/100] precision
        + tess_run(8350, 1, 8, 20)
    ),
    segments=[
        seg(200, 2700, [("CABINET", 4), ("CRATE", 1), ("MONITOR", 1)], (52, 80)),                       # [04/006]
        seg(2900, 4400, [("TRUCK", 5), ("CABINET", 2), ("CRATE", 1)], (50, 78)),
        seg(4400, 4990, [("DOOR", 4)], (70, 100)),                                                      # [14/039]
        seg(5100, 6000, [("DOOR", 3), ("TRUCK", 2), ("CABINET", 2)], (52, 78)),
        seg(6000, 7400, [("MONITOR", 3), ("DOOR", 3), ("CABINET", 3), ("TRUCK", 1)], (46, 70)),
    ])

LEVEL_DATA[(4, 2)] = scene(
    "The Void", "chase", "void",
    tip="Don't over-dash: President Loki's thugs are waiting!  Then: hoverboard!",
    spawns=([(3300, "EV_DASH", 0, 300), (5600, "EV_HOVER", 0, 3000)] + tess_run(8350, 1, 8, 20)),
    segments=[
        seg(200, 3200, [("RUINS", 4), ("PIRATE", 2), ("CRATE", 1)], (56, 86)),                         # [08/008]
        seg(3400, 5500, [("THUG", 6), ("RUINS", 1)], (36, 58), tess=.08),                              # [21/029]
        seg(5700, 8300, [("THUG", 3), ("COPTER", 3), ("PIRATE", 2)], (30, 46), speed=1.0, tess=.1),     # hoverboard
    ])

LEVEL_DATA[(4, 3)] = scene(
    "The Alioth Chase", "reverse", "alioth", boss="alioth",
    tip="ALIOTH APPROACHES!  Change lanes FAST. Survive!",
    segments=[
        seg(300, 2500, pattern="safe_seq", seq="random", hazard="COPTER", every=82, fill=["PIRATE", "NET"],
            speed=1.0, tess=.5, fill_every=4),
        seg(2500, 5500, pattern="safe_seq", seq="random", hazard="COPTER", every=68, fill=["ARCH", "BARRICADE"],
            speed=1.15, tess=.5, fill_every=3),
        seg(5500, 8300, pattern="safe_seq", seq="random", hazard="PIRATE", every=60, fill=["NET", "ARCH", "SPIKES"],
            speed=1.25, tess=.5, fill_every=3),
    ])

STAGE_NAMES = {1: "BATTLE OF NEW YORK (2012)", 2: "ASGARD & THE BIFROST",
               3: "SAKAAR WASTELANDS", 4: "THE TVA & THE VOID AT THE END OF TIME"}


class Level:
    """Result of load_level(): everything the engine needs for one scene."""

    def __init__(self, stage, scene_no, data, spawns):
        self.stage, self.scene_no = stage, scene_no
        self.name, self.cam, self.boss, self.tip = data["name"], data["cam"], data["boss"], data["tip"]
        self.theme = THEMES[data["theme"]]
        self.spawns = spawns
        self.length = SCENE_FRAMES
        self.checkpoint = int(SCENE_FRAMES * 0.55)
        self.reverse = data["cam"] == "reverse"
        r = random.Random(stage * 31 + scene_no)
        self.skyline = [(r.randrange(-40, W), r.randint(18, 40), r.randint(30, 110)) for _ in range(34)]


def load_level(stage, scene_no):
    """Build the sorted spawn list for (stage, scene) from LEVEL_DATA."""
    data = LEVEL_DATA[(stage, scene_no)]
    rng = random.Random(stage * 1000 + scene_no)      # deterministic procedural content
    spawns = list(data["spawns"])
    for s in data["segments"]:
        spawns += expand_segment(rng, s)
    spawns = [sp for sp in spawns if sp[0] < SCENE_FRAMES]
    spawns.sort(key=lambda t: t[0])
    return Level(stage, scene_no, data, spawns)


# ----------------------------------------------------------------------------
# PSEUDO-3D PROJECTION
# ----------------------------------------------------------------------------
def gp(t):
    return max(t, 0.0) ** 1.8


def rw(p):
    return 70 + (W * 0.95 - 70) * p


def py(p):
    return HORIZON + (BOTTOM - HORIZON) * p


def lane_x(p, lane):
    return W / 2 + (lane - 1) * rw(p) / 3


def obstacle_p(d, reverse):
    """Depth d (1 = spawn, 0 = player) -> screen scale p (0 = horizon, 1 = bottom)."""
    if reverse:
        t = T_R + (1 - T_R) * d if d >= 0 else T_R * (1 + d)
        return gp(t)
    return gp(T_N * (1 - d))


# ----------------------------------------------------------------------------
# SMALL HELPERS
# ----------------------------------------------------------------------------
_fonts = {}


def font(size):
    f = _fonts.get(size)
    if f is None:
        f = _fonts[size] = pygame.font.Font(None, size)
    return f


def text(surf, s, size, color, pos, anchor="center", outline=True):
    img = font(size).render(s, True, color)
    r = img.get_rect()
    setattr(r, anchor, pos)
    if outline:
        shade = font(size).render(s, True, (0, 0, 0))
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            surf.blit(shade, r.move(dx, dy))
    surf.blit(img, r)
    return r


def shade(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


def load_save():
    d = dict(cleared=False, expert_cleared=False, max_stage=1, skin="default")
    try:
        with open(SAVE_PATH) as fh:
            d.update(json.load(fh))
    except Exception:
        pass
    return d


def write_save(d):
    try:
        with open(SAVE_PATH, "w") as fh:
            json.dump(d, fh)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# ENTITIES
# ----------------------------------------------------------------------------
class Ob:
    __slots__ = ("name", "sp", "lane", "d", "speed", "dir", "phase", "done", "broken")

    def __init__(self, name, lane, speed):
        self.name, self.sp = name, OB[name]
        self.lane, self.speed = lane, speed
        self.d = 1.0
        self.dir = 1
        self.phase = 0.0
        self.done = False
        self.broken = False


class Particle:
    __slots__ = ("x", "y", "vx", "vy", "life", "color", "size")

    def __init__(self, x, y, vx, vy, life, color, size):
        self.x, self.y, self.vx, self.vy, self.life, self.color, self.size = x, y, vx, vy, life, color, size


class Controls:
    """Edge-triggered presses (cleared every frame) + held keys."""

    def __init__(self):
        self.left = self.right = self.jump = self.slide = self.dash = False
        self.up_held = self.down_held = self.space_held = self.brake_held = False

    def clear_edges(self):
        self.left = self.right = self.jump = self.slide = self.dash = False


# ----------------------------------------------------------------------------
# GAME
# ----------------------------------------------------------------------------
class Game:
    def __init__(self, screen, start_stage=1, start_scene=1):
        self.screen = screen
        self.canvas = pygame.Surface((W, H))
        self.fx = pygame.Surface((W, H), pygame.SRCALPHA)
        self.fx2 = pygame.Surface((W, H), pygame.SRCALPHA)
        self.state = "TITLE"
        self.save = load_save()
        self.skin = self.save["skin"] if self.save["skin"] in SKINS else "default"
        self.expert = False
        self.menu_idx = 0
        self.stage_sel = max(1, min(start_stage, 4))
        self.dbg_scene = start_scene
        self.ctl = Controls()
        self.frame = 0
        self.paused = False
        self.running = True
        self.stage = 1
        self.scene_no = 1
        self.lives = 3
        self.hp = 3
        self.max_hp = 5
        self.particles = []
        self.floaters = []              # (text, x, y, life, color)
        self.level = None
        self.sky_surf = None
        self.stage_stats = {}
        self.unlock_msgs = []
        self.victory_t = 0

    # ---- game flow --------------------------------------------------------
    def begin_game(self, stage, scene_no=1):
        self.stage, self.scene_no = stage, scene_no
        self.lives = 3
        self.max_hp = 5
        self.hp = 2 if self.expert else 3
        self.unlock_msgs = []
        self.start_scene()

    def start_scene(self):
        self.level = load_level(self.stage, self.scene_no)
        self.sky_surf = self.make_sky(self.level.theme["sky"])
        self.clock = 0.0
        self.rt = 0                      # real frames in scene
        self.spawn_idx = 0
        self.obs = []
        self.px = 1.0
        self.lane = 1
        self.jt = 0
        self.jt_total = 44
        self.slide_t = 0
        self.dash_t = 0
        self.dash_cd = 0
        self.brake_t = 0
        self.air = 0.0
        self.invuln = 0
        self.shake = 0
        self.spd = 1.0
        self.tess = 0
        self.tess_scene = 0
        self.hits = 0
        self.events = {}                 # effect/name -> end rt
        self.banner = ("", 0)
        self.particles = []
        self.floaters = []
        self.intro_t = 200
        self.hp = min(self.hp, self.max_hp)
        self.state = "PLAYING"
        self.paused = False

    def make_sky(self, cols):
        s = pygame.Surface((W, HORIZON + 40))
        top, bot = cols
        for y in range(HORIZON + 40):
            k = y / (HORIZON + 40)
            pygame.draw.line(s, tuple(int(top[i] + (bot[i] - top[i]) * k) for i in range(3)), (0, y), (W, y))
        return s

    def respawn(self):
        """Lose a life and restart from the last checkpoint."""
        self.lives -= 1
        self.hp = 3 if not self.expert else 2
        self.clock = float(self.level.checkpoint) if self.clock >= self.level.checkpoint else 0.0
        self.spawn_idx = 0
        sp = self.level.spawns
        while self.spawn_idx < len(sp) and sp[self.spawn_idx][0] < self.clock:
            self.spawn_idx += 1
        self.obs = []
        self.events = {}
        self.invuln = 150
        self.spd = 1.0
        self.dash_t = self.slide_t = self.jt = 0
        self.intro_t = 90
        self.floaters.append(("CHECKPOINT!", W // 2, 250, 90, (255, 255, 120)))

    def finish_scene(self):
        self.stage_stats = dict(tess=self.tess_scene, time=self.rt / FPS, hits=self.hits,
                                stage=self.stage, scene=self.scene_no)
        if self.scene_no == 3:
            self.save["max_stage"] = max(self.save.get("max_stage", 1), min(4, self.stage + 1))
            write_save(self.save)
        if self.stage == 4 and self.scene_no == 3:
            self.win_game()
        else:
            self.state = "STAGE_CLEAR"

    def win_game(self):
        self.unlock_msgs = []
        if not self.save["cleared"]:
            self.save["cleared"] = True
            self.unlock_msgs.append("UNLOCKED: Classic Loki skin + EXPERT MODE!")
        if self.expert and not self.save["expert_cleared"]:
            self.save["expert_cleared"] = True
            self.unlock_msgs.append("UNLOCKED: Frost Giant Loki skin!")
        write_save(self.save)
        self.state = "VICTORY"
        self.victory_t = 0

    def next_scene(self):
        if self.scene_no == 3:
            self.stage += 1
            self.scene_no = 1
        else:
            self.scene_no += 1
        self.start_scene()

    def unlocked_skins(self):
        out = ["default"]
        if self.save["cleared"]:
            out.append("classic")
        if self.save["expert_cleared"]:
            out.append("frost")
        return out

    # ---- events / input ---------------------------------------------------
    def handle_event(self, e):
        if e.type == pygame.QUIT:
            self.running = False
            return
        if e.type != pygame.KEYDOWN:
            return
        k = e.key
        c = self.ctl
        if self.state == "TITLE":
            if k in (pygame.K_RETURN, pygame.K_SPACE):
                self.state = "MENU"
            elif k == pygame.K_ESCAPE:
                self.running = False
        elif self.state == "MENU":
            self.menu_key(k)
        elif self.state == "PLAYING":
            if k == pygame.K_ESCAPE:
                self.state = "MENU"
            elif k == pygame.K_p:
                self.paused = not self.paused
            elif k in (pygame.K_LEFT, pygame.K_a):
                c.left = True
            elif k in (pygame.K_RIGHT, pygame.K_d):
                c.right = True
            elif k in (pygame.K_UP, pygame.K_w):
                c.jump = True
            elif k in (pygame.K_DOWN, pygame.K_s):
                c.slide = True
            elif k in (pygame.K_SPACE, pygame.K_z):
                c.dash = True
        elif self.state == "STAGE_CLEAR":
            if k in (pygame.K_RETURN, pygame.K_SPACE):
                self.next_scene()
        elif self.state in ("GAME_OVER", "VICTORY"):
            if k in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_ESCAPE):
                self.state = "MENU"

    def read_held(self):
        k = pygame.key.get_pressed()
        c = self.ctl
        c.up_held = k[pygame.K_UP] or k[pygame.K_w]
        c.down_held = k[pygame.K_DOWN] or k[pygame.K_s]
        c.space_held = k[pygame.K_SPACE] or k[pygame.K_z]
        c.brake_held = k[pygame.K_LSHIFT] or k[pygame.K_b]

    def menu_items(self):
        items = ["START GAME", "STAGE: %d" % self.stage_sel,
                 "SKIN: " + SKINS[self.skin][0],
                 "EXPERT MODE: " + ("ON" if self.expert else "OFF") + ("" if self.save["cleared"] else " (LOCKED)"),
                 "QUIT"]
        return items

    def menu_key(self, k):
        n = 5
        if k == pygame.K_UP:
            self.menu_idx = (self.menu_idx - 1) % n
        elif k == pygame.K_DOWN:
            self.menu_idx = (self.menu_idx + 1) % n
        elif k in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_RETURN, pygame.K_SPACE):
            dirn = -1 if k == pygame.K_LEFT else 1
            i = self.menu_idx
            if i == 0 and k in (pygame.K_RETURN, pygame.K_SPACE):
                self.begin_game(self.stage_sel, self.dbg_scene if self.stage_sel else 1)
                self.dbg_scene = 1
            elif i == 1:
                self.stage_sel = (self.stage_sel - 1 + dirn) % self.save["max_stage"] + 1
            elif i == 2:
                sk = self.unlocked_skins()
                self.skin = sk[(sk.index(self.skin) + dirn) % len(sk)] if self.skin in sk else "default"
                self.save["skin"] = self.skin
                write_save(self.save)
            elif i == 3 and self.save["cleared"]:
                self.expert = not self.expert
            elif i == 4 and k in (pygame.K_RETURN, pygame.K_SPACE):
                self.running = False
        elif k == pygame.K_ESCAPE:
            self.state = "TITLE"

    # ---- spawning ---------------------------------------------------------
    def spawn(self, fr, name, lane, speed):
        if name.startswith("EV_"):
            text_, effect = EVENTS[name]
            end = self.rt + int(speed)
            self.banner = (text_, self.rt + min(int(speed), 240))
            if effect:
                self.events[effect] = end
            return
        sp = OB[name]
        ob = Ob(name, 1 if lane < 0 else lane, speed * sp.spd)
        if lane < 0 and sp.full:
            ob.lane = 1
        if sp.lat == "cross":
            ob.dir = 1 if int(fr) % 2 == 0 else -1
            appr = APPROACH / ob.speed
            ob.lane = (1 if lane < 0 else lane) - ob.dir * sp.latspd * appr
        elif sp.lat == "bounce":
            ob.phase = random.random() * 6.28
        self.obs.append(ob)

    # ---- per-frame update -------------------------------------------------
    def update(self):
        self.frame += 1
        if self.state == "PLAYING" and not self.paused:
            self.update_play()
        elif self.state == "VICTORY":
            self.victory_t += 1
        self.ctl.clear_edges()

    def event_on(self, name):
        return self.events.get(name, 0) > self.rt

    def update_play(self):
        c = self.ctl
        lvl = self.level
        self.rt += 1
        if self.intro_t > 0:
            self.intro_t -= 1
        collar, hover, barrel = self.event_on("collar"), self.event_on("hover"), self.event_on("barrel")

        # ---- dash / brake / jump / slide
        if self.dash_cd > 0:
            self.dash_cd -= 1
        if self.dash_t > 0:
            self.dash_t -= 1
            if self.dash_t == 0:
                self.dash_cd = 45
        if self.brake_t > 0:
            self.brake_t -= 1
        want_dash = False
        if c.dash:
            if c.down_held:
                self.brake_t = 45               # DOWN + DASH = BRAKE
            else:
                want_dash = True
        if (c.jump and c.down_held) or (c.slide and c.up_held):
            want_dash = True                    # UP + SLIDE = DASH
            c.jump = c.slide = False
        if want_dash and self.dash_t == 0 and self.dash_cd == 0 and not hover:
            self.dash_t = 75
            self.floaters.append(("DASH!", int(lane_x(self.pp(), self.px)), int(py(self.pp())) - 150, 40, (255, 240, 120)))
        braking = (c.brake_held or self.brake_t > 0) and not hover and self.dash_t == 0
        if c.jump and self.jt <= 0 and self.slide_t <= 0 and not hover and not barrel:
            self.jt_total = 64 if self.dash_t > 0 else 44
            self.jt = self.jt_total
        if c.slide and self.slide_t <= 0 and self.jt <= 0 and not hover:
            self.slide_t = 34
        if self.jt > 0:
            self.air = math.sin(math.pi * (1 - self.jt / self.jt_total))
            self.jt -= 1
            if self.jt == 0:
                self.air = 0.0
        if self.slide_t > 0:
            self.slide_t -= 1
        sliding = self.slide_t > 0 or barrel

        # ---- lane
        move = (1 if c.right else 0) - (1 if c.left else 0)
        if collar:
            move = -move
        self.lane = max(0, min(2, self.lane + move))
        sp_lane = 0.32 if hover else 0.2
        self.px += max(-sp_lane, min(sp_lane, self.lane - self.px))

        # ---- world speed
        base = (1.0 + 0.06 * (self.stage - 1)) * (1.2 if self.expert else 1.0)
        target = base
        if self.dash_t > 0:
            target = base * 1.6
        elif braking:
            target = 0.12
        elif hover:
            target = base * 1.35
        self.spd += (target - self.spd) * (0.25 if braking else 0.12)
        self.braking = braking
        self.clock += self.spd

        # ---- spawns
        sp = lvl.spawns
        while self.spawn_idx < len(sp) and sp[self.spawn_idx][0] <= self.clock:
            self.spawn(*sp[self.spawn_idx])
            self.spawn_idx += 1

        # ---- obstacles
        rate = 1.0 / APPROACH
        for ob in self.obs:
            ob.d -= rate * self.spd * ob.speed
            if ob.sp.lat == "cross":
                ob.lane += ob.dir * ob.sp.latspd
            elif ob.sp.lat == "bounce":
                ob.phase += 0.045
                ob.lane = 1 + 1.15 * math.sin(ob.phase)
            if not ob.done:
                self.check_collision(ob, sliding)
        lim = -0.95 if lvl.reverse else -0.3
        self.obs = [o for o in self.obs if o.d > lim and not (o.done and o.sp.kind == "pickup")
                    and not (o.broken)]

        # ---- timers & fx
        if self.invuln > 0:
            self.invuln -= 1
        if self.shake > 0:
            self.shake -= 1
        for p in self.particles:
            p.x += p.vx
            p.y += p.vy
            p.vy += 0.25
            p.life -= 1
        self.particles = [p for p in self.particles if p.life > 0]
        self.floaters = [(t, x, y - 1, l - 1, col) for (t, x, y, l, col) in self.floaters if l > 0]

        # ---- end conditions
        if self.hp <= 0:
            if self.lives <= 0:
                self.state = "GAME_OVER"
            else:
                self.respawn()
        elif self.clock >= lvl.length:
            self.finish_scene()

    def pp(self):
        return P_R if self.level.reverse else P_N

    def check_collision(self, ob, sliding):
        sp = ob.sp
        if not (-sp.depth <= ob.d <= 0.03):
            if ob.d < -sp.depth:
                ob.done = True
            return
        if sp.full:
            hit = True
        else:
            hit = abs(ob.lane - self.px) < 0.28 + sp.wf * 0.35
        if not hit:
            return
        kind = sp.kind
        if kind == "pickup":
            if self.air < 0.75:
                ob.done = True
                self.collect()
            return
        if kind == "jump":
            safe = self.air > 0.28
        elif kind == "gap":
            safe = self.air > 0.22
        elif kind == "slide":
            safe = sliding
        elif kind == "dash":
            safe = self.dash_t > 0
            if safe:
                ob.broken = True
                self.burst(ob, 26)
                self.shake = max(self.shake, 8)
                self.floaters.append(("SMASH!", int(lane_x(self.pp(), self.px)), 300, 40, (255, 200, 80)))
                return
        else:
            safe = False
        if safe or self.invuln > 0:
            if not safe:
                return
            return
        ob.done = True
        self.take_hit(ob)

    def take_hit(self, ob):
        self.hp -= 1
        self.hits += 1
        self.invuln = 110
        self.shake = 18
        self.spd *= 0.4
        self.dash_t = 0
        self.burst(ob, 22)
        self.floaters.append(("OUCH!", int(lane_x(self.pp(), self.px)), int(py(self.pp())) - 160, 50, (255, 90, 90)))

    def burst(self, ob, n):
        x = lane_x(self.pp(), self.px)
        y = py(self.pp()) - 60
        for _ in range(n):
            a = random.random() * 6.28
            v = 2 + random.random() * 5
            self.particles.append(Particle(x, y, math.cos(a) * v, math.sin(a) * v - 3,
                                           random.randint(18, 36), ob.sp.color, random.randint(3, 7)))

    def collect(self):
        self.tess += 1
        self.tess_scene += 1
        x = int(lane_x(self.pp(), self.px))
        y = int(py(self.pp())) - 120
        if self.tess % 10 == 0 and self.hp < self.max_hp:
            self.hp += 1
            self.floaters.append(("+1 HEALTH", x, y, 60, (255, 120, 120)))
        if self.tess % 20 == 0 and self.lives < 9:
            self.lives += 1
            self.floaters.append(("EXTRA LIFE!", x, y - 30, 80, (120, 255, 120)))
        for _ in range(4):
            self.particles.append(Particle(x, y + 60, random.uniform(-2, 2), random.uniform(-4, -1), 20,
                                           (110, 220, 255), 3))

    # ========================================================================
    # DRAWING
    # ========================================================================
    def draw(self):
        s = self.canvas
        st = self.state
        if st == "TITLE":
            self.draw_title(s)
        elif st == "MENU":
            self.draw_menu(s)
        elif st in ("PLAYING", "STAGE_CLEAR", "GAME_OVER", "VICTORY"):
            if self.level is not None:
                self.draw_world(s)
                self.draw_hud(s)
            if st == "STAGE_CLEAR":
                self.draw_clear(s)
            elif st == "GAME_OVER":
                self.draw_game_over(s)
            elif st == "VICTORY":
                self.draw_victory(s)
            elif self.paused:
                text(s, "PAUSED", 90, (255, 255, 255), (W // 2, H // 2))
        ox = oy = 0
        if self.state == "PLAYING" and self.shake > 0:
            ox, oy = random.randint(-6, 6), random.randint(-6, 6)
        self.screen.fill((0, 0, 0))
        self.screen.blit(s, (ox, oy))
        pygame.display.flip()

    # ---- world ------------------------------------------------------------
    def draw_world(self, s):
        lvl = self.level
        th = lvl.theme
        rev = lvl.reverse
        cam_off = (self.px - 1) * 22
        s.fill(th["ground"])
        s.blit(self.sky_surf, (0, 0))
        # skyline
        for (x, w, h) in lvl.skyline:
            pygame.draw.rect(s, th["sky_line"], (x - cam_off * 0.5, HORIZON - h, w, h + 2))
        # ground + road
        pygame.draw.rect(s, th["ground"], (0, HORIZON, W, H - HORIZON))
        road = [(lane_x(0, 1) - rw(0) / 2, HORIZON), (lane_x(0, 1) + rw(0) / 2, HORIZON),
                (W / 2 + rw(1) / 2, BOTTOM), (W / 2 - rw(1) / 2, BOTTOM)]
        pygame.draw.polygon(s, th["road"], road)
        pygame.draw.line(s, shade(th["road"], 1.4), road[0], road[3], 3)
        pygame.draw.line(s, shade(th["road"], 1.4), road[1], road[2], 3)
        # moving road marks + scenery
        off = (self.clock * 0.0065) % 1.0
        n = 14
        for i in range(n):
            u = ((i / n) + (-off if rev else off)) % 1.0
            p = gp(u * 1.1)
            if p < 0.004:
                continue
            y = py(p)
            lw = rw(p) / 3
            for k in (-0.5, 0.5):
                x = W / 2 + k * lw
                pygame.draw.rect(s, th["line"], (x - max(1, lw * 0.025), y - lw * 0.3, max(2, lw * 0.05), lw * 0.3))
            # side props
            hh = lw * (0.9 + ((i * 7) % 5) * 0.25)
            for sgn in (-1, 1):
                x = W / 2 + sgn * (rw(p) / 2 + lw * 0.7)
                pygame.draw.rect(s, th["side"], (x - lw * 0.3, y - hh, lw * 0.6, hh))
                pygame.draw.rect(s, shade(th["side"], 1.3), (x - lw * 0.3, y - hh, lw * 0.6, max(2, hh * 0.12)))
        if rev and lvl.boss:
            self.draw_boss(s, lvl.boss)
        # dash speed-lines
        if self.dash_t > 0 or self.spd > 1.3:
            for _ in range(14):
                a = random.random() * 6.28
                r1, r2 = 60 + random.random() * 120, 220 + random.random() * 320
                pygame.draw.line(s, (255, 255, 255), (W / 2 + math.cos(a) * r1, HORIZON + math.sin(a) * r1 * 0.5),
                                 (W / 2 + math.cos(a) * r2, HORIZON + math.sin(a) * r2 * 0.9), 1)
        # entities sorted far -> near
        pplayer = self.pp()
        items = [(obstacle_p(o.d, rev), 0, o) for o in self.obs]
        items.append((pplayer, 1, None))
        items.sort(key=lambda t: t[0])
        near = None
        for p, isplayer, o in items:
            if isplayer:
                self.draw_player(s)
            elif p > 0.002:
                if rev and p > pplayer + 0.02:
                    # lead cam: objects between camera and Loki are ghosted so he stays visible
                    if near is None:
                        near = self.fx2
                        near.fill((0, 0, 0, 0))
                    self.draw_obstacle(near, o, p)
                else:
                    self.draw_obstacle(s, o, p)
        if near is not None:
            near.set_alpha(150)
            s.blit(near, (0, 0))
        # particles
        for p in self.particles:
            pygame.draw.rect(s, p.color, (p.x, p.y, p.size, p.size))
        for (t, x, y, l, col) in self.floaters:
            text(s, t, 34, col, (x, y))
        # Event overlays
        if self.event_on("collar") and (self.rt // 10) % 2 == 0:
            pygame.draw.rect(s, (255, 60, 60), (0, 0, W, H), 6)
        if self.event_on("hover"):
            pygame.draw.rect(s, (80, 200, 255), (0, 0, W, H), 4)

    def draw_obstacle(self, s, o, p):
        sp = o.sp
        y = py(p)
        lw = rw(p) / 3
        if sp.full:
            cx = W / 2
            w = rw(p) * 0.98
        else:
            cx = lane_x(p, o.lane)
            w = lw * sp.wf
        h = lw * sp.hf
        col = sp.color
        flash = o.d < 0.0 and sp.kind == "pickup"
        if sp.style == "tess":
            bob = math.sin(self.frame * 0.12 + o.d * 20) * lw * 0.06
            r = max(3, lw * 0.17)
            cy = y - lw * 0.32 + bob
            glow = pygame.Surface((int(r * 6), int(r * 6)), pygame.SRCALPHA)
            pygame.draw.circle(glow, (80, 200, 255, 70), (int(r * 3), int(r * 3)), int(r * 3))
            s.blit(glow, (cx - r * 3, cy - r * 3))
            pts = [(cx, cy - r * 1.5), (cx + r * 1.1, cy), (cx, cy + r * 1.5), (cx - r * 1.1, cy)]
            pygame.draw.polygon(s, (130, 235, 255), pts)
            pygame.draw.polygon(s, (255, 255, 255), pts, max(1, int(r * 0.2)))
            return
        if sp.style == "gap":
            depth_px = (py(obstacle_p(o.d + sp.depth, self.level.reverse)) - y) if not self.level.reverse else lw * 0.5
            hh = max(4, abs(depth_px))
            pygame.draw.rect(s, col, (cx - w / 2, y - hh, w, hh))
            pygame.draw.rect(s, shade(col, 2.2), (cx - w / 2, y - hh, w, hh), max(1, int(lw * 0.02)))
            for i in range(1, 4):
                pygame.draw.line(s, shade(col, 1.8), (cx - w / 2, y - hh * i / 4), (cx + w / 2, y - hh * i / 4), 1)
            self.label(s, o, cx, y - hh - 4, lw)
            return
        if sp.style == "beam":
            bw = max(3, w * 0.55)
            pulse = 0.75 + 0.25 * math.sin(self.frame * 0.4)
            glow = pygame.Surface((int(bw * 2.6) + 2, int(h) + 2), pygame.SRCALPHA)
            glow.fill((*col, int(80 * pulse)))
            s.blit(glow, (cx - bw * 1.3, y - h))
            pygame.draw.rect(s, shade(col, pulse), (cx - bw / 2, y - h, bw, h))
            pygame.draw.rect(s, (255, 255, 255), (cx - bw * 0.18, y - h, bw * 0.36, h))
            self.label(s, o, cx, y - h - 4, lw)
            return
        if sp.style == "oh":
            lift = lw * 0.62
            top = y - lift - h
            legw = max(2, w * 0.07)
            pygame.draw.rect(s, shade(col, .55), (cx - w / 2, top, legw, lift + h))
            pygame.draw.rect(s, shade(col, .55), (cx + w / 2 - legw, top, legw, lift + h))
            pygame.draw.rect(s, col, (cx - w / 2, top, w, h))
            pygame.draw.rect(s, shade(col, 1.4), (cx - w / 2, top, w, max(2, h * .2)))
            pygame.draw.rect(s, shade(col, .5), (cx - w / 2, top, w, h), max(1, int(lw * .015)))
            for i in range(1, 5):          # hazard stripes
                x = cx - w / 2 + w * i / 5
                pygame.draw.line(s, shade(col, .6), (x, top), (x - h * .4, top + h), max(1, int(lw * .02)))
            self.label(s, o, cx, top - 4, lw)
            return
        # box / brick
        pygame.draw.rect(s, col, (cx - w / 2, y - h, w, h))
        pygame.draw.rect(s, shade(col, 1.35), (cx - w / 2, y - h, w, max(2, h * .16)))
        pygame.draw.rect(s, shade(col, .55), (cx - w / 2, y - h * .14, w, h * .14))
        pygame.draw.rect(s, shade(col, .45), (cx - w / 2, y - h, w, h), max(1, int(lw * .015)))
        if sp.style == "brick":
            rows = 4
            for r in range(1, rows):
                yy = y - h + h * r / rows
                pygame.draw.line(s, shade(col, .55), (cx - w / 2, yy), (cx + w / 2, yy), max(1, int(lw * .012)))
                for k in range(3):
                    xx = cx - w / 2 + w * (k + 0.5 * (r % 2)) / 3
                    pygame.draw.line(s, shade(col, .55), (xx, yy), (xx, yy + h / rows), max(1, int(lw * .012)))
        if sp.lat:
            for k in range(3):             # motion streaks
                yy = y - h * (0.25 + 0.25 * k)
                pygame.draw.line(s, (255, 255, 255), (cx - o.dir * (w / 2 + 6), yy),
                                 (cx - o.dir * (w / 2 + 6 + lw * .25), yy), 1)
        self.label(s, o, cx, y - h - 4, lw)

    def label(self, s, o, cx, y, lw):
        if lw > 60 and o.sp.label and o.d > -0.05:
            size = int(max(16, min(26, lw * 0.12)))
            text(s, o.sp.label, size, (255, 255, 255), (cx, y), anchor="midbottom")

    # ---- Loki -------------------------------------------------------------
    def draw_player(self, s):
        p = self.pp()
        lw = rw(p) / 3
        sc = lw / 263.0
        x = lane_x(p, self.px)
        fy = py(p)
        front = self.level.reverse
        if self.invuln > 0 and (self.invuln // 4) % 2 == 0:
            return
        hover = self.event_on("hover")
        barrel = self.event_on("barrel")
        sliding = self.slide_t > 0 or barrel
        # afterimages while dashing
        if self.dash_t > 0:
            for i in range(3, 0, -1):
                draw_loki(s, x, fy - i * 14 * sc, sc * (1 - i * .03), self.skin, self.frame, self.air, sliding, True,
                          False, front, ghost=70 - i * 18)
        braking = getattr(self, "braking", False)
        draw_loki(s, x, fy, sc, self.skin, self.frame, self.air, sliding, self.dash_t > 0, braking, front,
                  board=hover, barrel=barrel)

    # ---- bosses (reverse camera) ------------------------------------------
    def draw_boss(self, s, kind):
        t = self.frame
        cx = W // 2
        fx = self.fx
        fx.fill((0, 0, 0, 0))
        if kind == "hulk":
            sway = math.sin(t * 0.08) * 10
            pygame.draw.rect(s, (120, 62, 150), (cx - 190, 150, 380, 120))
            pygame.draw.ellipse(s, (78, 168, 66), (cx - 230, -150, 460, 360))
            for sgn in (-1, 1):
                ang = math.sin(t * 0.06 + (0 if sgn < 0 else math.pi))
                ax = cx + sgn * (270 + ang * 50)
                ay = 120 + ang * 40
                pygame.draw.line(s, (68, 148, 58), (cx + sgn * 180, 10), (ax, ay), 70)
                pygame.draw.circle(s, (84, 172, 70), (int(ax), int(ay + 20)), 54)
            pygame.draw.circle(s, (92, 184, 80), (cx, int(55 + sway)), 66)
            for sgn in (-1, 1):
                ex = cx + sgn * 26
                pygame.draw.circle(s, (255, 255, 255), (ex, int(48 + sway)), 11)
                pygame.draw.circle(s, (255, 40, 40), (ex, int(50 + sway)), 5)
                pygame.draw.polygon(s, (40, 100, 40), [(ex - sgn * 22, 26 + sway), (ex + sgn * 6, 40 + sway),
                                                       (ex + sgn * 8, 30 + sway)])
            pygame.draw.rect(s, (30, 60, 30), (cx - 30, 85 + sway, 60, 12))
            text(s, "H U L K   S M A S H !", 30, (255, 255, 120), (cx, 215))
        elif kind == "destroyer":
            pygame.draw.rect(s, (110, 115, 128), (cx - 180, -30, 360, 250))
            pygame.draw.rect(s, (80, 84, 96), (cx - 180, -30, 360, 250), 6)
            for i in range(5):
                pygame.draw.line(s, (70, 74, 86), (cx - 180, 20 + i * 42), (cx + 180, 20 + i * 42), 3)
            pygame.draw.rect(s, (40, 42, 52), (cx - 120, 70, 240, 34))
            glow = 150 + int(80 * math.sin(t * 0.3))
            pygame.draw.rect(s, (255, glow, 40), (cx - 110, 80, 220, 14))
            pygame.draw.polygon(s, (90, 94, 108), [(cx - 70, 150), (cx + 70, 150), (cx + 40, 215), (cx - 40, 215)])
            pygame.draw.circle(fx, (255, 140, 40, 120), (cx, 200), 50 + int(10 * math.sin(t * 0.4)))
            s.blit(fx, (0, 0))
            text(s, "D E S T R O Y E R", 30, (255, 200, 120), (cx, 235))
        elif kind == "grandmaster":
            pygame.draw.ellipse(fx, (210, 110, 235, 150), (cx - 190, -90, 380, 340))
            for y in range(0, 260, 7):
                pygame.draw.line(fx, (60, 0, 80, 70), (cx - 190, y), (cx + 190, y), 2)
            pygame.draw.ellipse(fx, (90, 200, 255, 140), (cx - 60, -90, 120, 110))
            for sgn in (-1, 1):
                pygame.draw.ellipse(fx, (255, 255, 255, 190), (cx + sgn * 70 - 28, 55, 56, 26))
                pygame.draw.circle(fx, (30, 0, 60, 230), (cx + sgn * 70, 68), 9)
            pygame.draw.arc(fx, (255, 220, 255, 200), (cx - 70, 100, 140, 80), 3.5, 5.9, 5)
            s.blit(fx, (0, 0))
            text(s, "G R A N D M A S T E R", 30, (255, 220, 255), (cx, 235))
        elif kind == "alioth":
            for i in range(70):
                x = (i * 137) % (W + 200) - 100 + math.sin(t * 0.02 + i) * 25
                y = ((i * 71) % 290) - 60 + math.cos(t * 0.03 + i) * 18
                r = 55 + (i * 13) % 55
                c = (70 + (i * 9) % 60, 10 + (i * 5) % 30, 110 + (i * 7) % 80, 150)
                pygame.draw.circle(fx, c, (int(x), int(y)), r)
            for sgn in (-1, 1):
                pygame.draw.circle(fx, (255, 180, 60, 120), (cx + sgn * 90, 80), 40)
                pygame.draw.circle(fx, (255, 240, 120, 230), (cx + sgn * 90, 80), 20)
            s.blit(fx, (0, 0))
            text(s, "A L I O T H", 34, (255, 150, 255), (cx, 235))

    # ---- HUD --------------------------------------------------------------
    def draw_hud(self, s):
        lvl = self.level
        # top-left: tesseract count + health bar
        pts = [(34, 20), (46, 34), (34, 48), (22, 34)]
        pygame.draw.polygon(s, (110, 225, 255), pts)
        pygame.draw.polygon(s, (255, 255, 255), pts, 2)
        text(s, "x %03d" % self.tess, 40, (150, 235, 255), (62, 34), anchor="midleft")
        text(s, "HP", 24, (255, 255, 255), (14, 70), anchor="midleft")
        for i in range(self.max_hp):
            r = pygame.Rect(46 + i * 30, 58, 26, 24)
            if i < self.hp:
                pygame.draw.rect(s, (230, 50, 60), r)
                pygame.draw.rect(s, (255, 150, 150), (r.x, r.y, r.w, 6))
            pygame.draw.rect(s, (255, 255, 255), r, 2)
        # top-middle: timer
        sec = int(self.rt / FPS)
        text(s, "%02d:%02d" % (sec // 60, sec % 60), 46, (255, 255, 255), (W // 2, 28))
        text(s, "STAGE %d-%d  %s" % (lvl.stage, lvl.scene_no, lvl.name.upper()), 22, (255, 230, 150), (W // 2, 58))
        # top-right: progress bar
        bx, by, bw = W - 270, 16, 250
        pygame.draw.rect(s, (20, 20, 30), (bx, by, bw, 18))
        prog = min(1.0, self.clock / lvl.length)
        pygame.draw.rect(s, (70, 200, 90), (bx, by, bw * prog, 18))
        pygame.draw.rect(s, (255, 255, 255), (bx, by, bw, 18), 2)
        cpx = bx + bw * lvl.checkpoint / lvl.length
        pygame.draw.line(s, (255, 230, 90), (cpx, by - 4), (cpx, by + 22), 3)
        mx = bx + bw * prog
        pygame.draw.polygon(s, SKINS[self.skin][2], [(mx, by + 26), (mx - 7, by + 40), (mx + 7, by + 40)])
        text(s, "GOAL", 20, (255, 255, 255), (bx + bw, by + 38), anchor="topright")
        # bottom-right: lives
        draw_loki_head(s, W - 120, H - 48, 0.6, self.skin)
        text(s, "x %d" % self.lives, 44, (255, 255, 255), (W - 80, H - 46), anchor="midleft")
        # dash meter
        if self.dash_t > 0:
            pygame.draw.rect(s, (255, 220, 80), (14, H - 26, 150 * self.dash_t / 75, 12))
        elif self.dash_cd > 0:
            pygame.draw.rect(s, (120, 120, 120), (14, H - 26, 150 * (1 - self.dash_cd / 45), 12))
        else:
            pygame.draw.rect(s, (90, 255, 120), (14, H - 26, 150, 12))
        pygame.draw.rect(s, (255, 255, 255), (14, H - 26, 150, 12), 2)
        text(s, "DASH", 20, (255, 255, 255), (170, H - 20), anchor="midleft")
        # banner + intro
        if self.banner[1] > self.rt:
            text(s, self.banner[0], 36, (255, 255, 120), (W // 2, 112))
        if self.intro_t > 0 and self.state == "PLAYING":
            text(s, "STAGE %d - SCENE %d" % (lvl.stage, lvl.scene_no), 64, (255, 255, 255), (W // 2, 240))
            text(s, lvl.name.upper() + ("   [LEAD CAM]" if lvl.reverse else ""), 40, (255, 230, 120), (W // 2, 290))
            text(s, lvl.tip, 26, (200, 255, 200), (W // 2, 330))

    # ---- screens ----------------------------------------------------------
    def backdrop(self, s, colors):
        top, bot = colors
        for y in range(0, H, 4):
            k = y / H
            pygame.draw.rect(s, tuple(int(top[i] + (bot[i] - top[i]) * k) for i in range(3)), (0, y, W, 4))

    def draw_title(self, s):
        self.backdrop(s, ((10, 40, 20), (60, 150, 80)))
        t = self.frame
        # perspective grid
        for i in range(-10, 11):
            pygame.draw.line(s, (40, 110, 60), (W // 2 + i * 20, 330), (W // 2 + i * 120, H), 2)
        for i in range(8):
            y = 330 + ((i * 36 + t) % 270) ** 1.0
            pygame.draw.line(s, (40, 110, 60), (0, y), (W, y), 1)
        draw_loki_head(s, W // 2, 190, 2.6, self.skin)
        text(s, "LOKIMAN 2", 130, (255, 215, 70), (W // 2, 330))
        text(s, "GLORIOUS PURPOSE.  MULTIVERSAL RUN.", 32, (210, 255, 210), (W // 2, 392))
        if (t // 30) % 2 == 0:
            text(s, "PRESS  ENTER", 48, (255, 255, 255), (W // 2, 470))
        text(s, "Arrows: move/jump/slide   SPACE: dash   SHIFT: brake   P: pause", 22, (230, 230, 230),
             (W // 2, 560))

    def draw_menu(self, s):
        self.backdrop(s, ((20, 10, 50), (70, 150, 90)))
        text(s, "LOKIMAN 2", 90, (255, 215, 70), (W // 2, 80))
        for i, it in enumerate(self.menu_items()):
            sel = i == self.menu_idx
            col = (255, 255, 120) if sel else (220, 220, 220)
            text(s, ("> " if sel else "  ") + it, 44, col, (W // 2, 190 + i * 62))
        name = STAGE_NAMES[self.stage_sel]
        text(s, "Stage %d: %s" % (self.stage_sel, name), 26, (180, 255, 200), (W // 2, 515))
        text(s, "UP/DOWN select   LEFT/RIGHT change   ENTER confirm", 22, (210, 210, 210), (W // 2, 560))
        draw_loki_head(s, 130, 300, 1.8, self.skin)
        draw_loki_head(s, W - 130, 300, 1.8, self.skin)

    def draw_clear(self, s):
        ov = pygame.Surface((W, H), pygame.SRCALPHA)
        ov.fill((0, 0, 0, 170))
        s.blit(ov, (0, 0))
        st = self.stage_stats
        text(s, "SCENE CLEAR!", 90, (255, 230, 90), (W // 2, 130))
        text(s, "Stage %d - Scene %d : %s" % (st["stage"], st["scene"], LEVEL_DATA[(st["stage"], st["scene"])]["name"]),
             34, (255, 255, 255), (W // 2, 200))
        sec = int(st["time"])
        for i, line in enumerate(("Tesseracts: %d" % st["tess"], "Time: %02d:%02d" % (sec // 60, sec % 60),
                                  "Hits taken: %d" % st["hits"], "Lives: %d" % self.lives)):
            text(s, line, 44, (150, 235, 255), (W // 2, 280 + i * 54))
        if st["scene"] == 3 and st["stage"] < 4:
            text(s, "STAGE %d UNLOCKED!" % (st["stage"] + 1), 40, (120, 255, 120), (W // 2, 510))
        if (self.frame // 30) % 2 == 0:
            text(s, "PRESS ENTER", 36, (255, 255, 255), (W // 2, 560))

    def draw_game_over(self, s):
        ov = pygame.Surface((W, H), pygame.SRCALPHA)
        ov.fill((60, 0, 0, 190))
        s.blit(ov, (0, 0))
        text(s, "GAME OVER", 130, (255, 70, 70), (W // 2, 230))
        text(s, "The Time Variance Authority has pruned you.", 34, (255, 255, 255), (W // 2, 330))
        if (self.frame // 30) % 2 == 0:
            text(s, "PRESS ENTER", 40, (255, 255, 255), (W // 2, 440))

    def draw_victory(self, s):
        self.backdrop(s, ((30, 0, 60), (255, 190, 60)))
        t = self.victory_t
        for i in range(40):
            x = (i * 97 + t * (1 + i % 3)) % W
            y = (i * 53 + t * 2) % H
            pygame.draw.rect(s, (255, 230, 120), (x, y, 4, 4))
        draw_loki_head(s, W // 2, 190, 3.0, self.skin)
        text(s, "MULTIVERSE SAVED...", 80, (255, 255, 255), (W // 2, 340))
        text(s, "KNEEL BEFORE YOUR KING!", 70, (255, 215, 70), (W // 2, 410))
        for i, m in enumerate(self.unlock_msgs):
            text(s, m, 32, (150, 255, 170), (W // 2, 470 + i * 34))
        if (self.frame // 30) % 2 == 0:
            text(s, "PRESS ENTER", 34, (255, 255, 255), (W // 2, 565))


# ----------------------------------------------------------------------------
# LOKI SPRITES (procedural, scale-aware)
# ----------------------------------------------------------------------------
def draw_loki_head(s, x, y, k, skin):
    """Front-facing horned helmet bust, used on menus and the HUD."""
    _, prim, sec, tone, horn = SKINS[skin]
    pygame.draw.polygon(s, horn, [(x - 20 * k, y - 8 * k), (x - 44 * k, y - 62 * k), (x - 10 * k, y - 22 * k)])
    pygame.draw.polygon(s, horn, [(x + 20 * k, y - 8 * k), (x + 44 * k, y - 62 * k), (x + 10 * k, y - 22 * k)])
    pygame.draw.polygon(s, horn, [(x, y - 24 * k), (x - 8 * k, y - 58 * k), (x + 8 * k, y - 58 * k)])
    pygame.draw.circle(s, (20, 20, 25), (int(x), int(y)), int(max(2, 24 * k)))
    pygame.draw.ellipse(s, tone, (x - 17 * k, y - 14 * k, 34 * k, 40 * k))
    pygame.draw.rect(s, sec, (x - 22 * k, y - 20 * k, 44 * k, 8 * k))
    pygame.draw.rect(s, prim, (x - 28 * k, y + 24 * k, 56 * k, 18 * k))
    for sgn in (-1, 1):
        pygame.draw.circle(s, (20, 40, 20), (int(x + sgn * 7 * k), int(y + 2 * k)), int(max(1, 2.2 * k)))
    pygame.draw.line(s, (120, 40, 40), (x - 6 * k, y + 14 * k), (x + 7 * k, y + 12 * k), max(1, int(2 * k)))


def draw_loki(s, x, fy, sc, skin, anim, air, sliding, dashing, braking, front, ghost=0, board=False, barrel=False):
    """Draw Loki standing with feet at (x, fy).  sc = scale (1 = 130 px tall)."""
    _, prim, sec, tone, horn = SKINS[skin]
    surf = s
    if ghost:
        surf = pygame.Surface((W, H), pygame.SRCALPHA)
    h = 130 * sc
    w = 62 * sc
    squash = 1.0
    if sliding:
        squash = 0.5
    elif braking:
        squash = 0.85
    h *= squash
    if sliding:
        w *= 1.4
    y = fy - air * 120 * sc
    # shadow
    pygame.draw.ellipse(s, (0, 0, 0), (x - w * .7, fy - 5 * sc, w * 1.4, 10 * sc))
    if board:
        pygame.draw.polygon(surf, (150, 80, 220), [(x - w * 1.0, y), (x + w * 1.0, y), (x + w * .7, y + 9 * sc),
                                                  (x - w * .7, y + 9 * sc)])
        pygame.draw.line(surf, (255, 200, 255), (x - w, y), (x + w, y), max(1, int(2 * sc)))
    lean = 0.18 if dashing else 0.0
    swing = 0 if (air > 0.05 or braking or sliding) else math.sin(anim * 0.45) * 15 * sc
    if air > 0.05:
        swing = 12 * sc
    legh = h * 0.42
    for sgn in (-1, 1):
        lx = x + sgn * w * .22 + sgn * swing * .3
        ly = y - legh + (swing * sgn * 0.5 if not sliding else 0)
        pygame.draw.rect(surf, shade(prim, .6), (lx - w * .13, ly, w * .26, legh))
        pygame.draw.rect(surf, (30, 30, 30), (lx - w * .16, y - 6 * sc, w * .32, 6 * sc))
    # cape (behind, back view) or side drapes (front view)
    top = y - h * 0.86
    if not front:
        pygame.draw.polygon(surf, shade(prim, .75), [(x - w * .55, top + h * .08), (x + w * .55, top + h * .08),
                                                    (x + w * .8 + swing * .3, y - h * .08),
                                                    (x - w * .8 + swing * .3, y - h * .08)])
        pygame.draw.polygon(surf, sec, [(x - w * .8 + swing * .3, y - h * .08), (x + w * .8 + swing * .3, y - h * .08),
                                       (x + w * .78 + swing * .3, y - h * .13), (x - w * .78 + swing * .3, y - h * .13)])
    else:
        for sgn in (-1, 1):
            pygame.draw.polygon(surf, shade(prim, .7), [(x + sgn * w * .45, top + h * .1), (x + sgn * w * .75, top + h * .12),
                                                       (x + sgn * w * .85, y - h * .1), (x + sgn * w * .4, y - h * .12)])
    # torso
    ty = top + h * .1
    pygame.draw.rect(surf, prim, (x - w * .45 + lean * w, ty, w * .9, h * .36))
    pygame.draw.rect(surf, sec, (x - w * .45 + lean * w, ty + h * .26, w * .9, h * .06))          # belt
    if front:
        pygame.draw.polygon(surf, sec, [(x - w * .3, ty), (x + w * .3, ty), (x, ty + h * .2)])      # gold chest
    # arms
    for sgn in (-1, 1):
        ax = x + sgn * w * .52 + lean * w
        ay = ty + h * .02 + swing * sgn * .4
        pygame.draw.rect(surf, shade(prim, 1.1), (ax - w * .09, ay, w * .18, h * .26))
        pygame.draw.circle(surf, tone, (int(ax), int(ay + h * .27)), int(max(2, w * .1)))
    # head
    hr = max(3, w * .27)
    hx, hy = x + lean * w * 1.4, top + h * .02
    if front:
        pygame.draw.circle(surf, tone, (int(hx), int(hy)), int(hr))
        for sgn in (-1, 1):
            pygame.draw.circle(surf, (30, 70, 30), (int(hx + sgn * hr * .38), int(hy - hr * .05)), max(1, int(hr * .13)))
        pygame.draw.line(surf, (140, 50, 50), (hx - hr * .3, hy + hr * .42), (hx + hr * .35, hy + hr * .35), max(1, int(hr * .1)))
        pygame.draw.circle(surf, (18, 18, 22), (int(hx), int(hy - hr * .5)), int(hr * .9), 0)
        pygame.draw.circle(surf, tone, (int(hx), int(hy + hr * .15)), int(hr * .85))
    else:
        pygame.draw.circle(surf, (18, 18, 22), (int(hx), int(hy)), int(hr))          # slicked black hair
    # helmet & horns
    pygame.draw.rect(surf, horn, (hx - hr * 1.05, hy - hr * .55, hr * 2.1, max(2, hr * .35)))
    pygame.draw.polygon(surf, horn, [(hx - hr * .9, hy - hr * .4), (hx - hr * 2.1, hy - hr * 2.7), (hx - hr * .3, hy - hr * .9)])
    pygame.draw.polygon(surf, horn, [(hx + hr * .9, hy - hr * .4), (hx + hr * 2.1, hy - hr * 2.7), (hx + hr * .3, hy - hr * .9)])
    pygame.draw.polygon(surf, horn, [(hx - hr * .3, hy - hr * .8), (hx, hy - hr * 2.2), (hx + hr * .3, hy - hr * .8)])
    if ghost:
        surf.set_alpha(ghost)
        s.blit(surf, (0, 0))


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="LokiMan2 - a Loki-themed Pepsiman-style runner")
    ap.add_argument("--stage", type=int, default=1, help="start stage selected in the menu (1-4)")
    ap.add_argument("--scene", type=int, default=1, help="debug: start scene (1-3) of the selected stage")
    ap.add_argument("--windowed", action="store_true", help="(default) kept for compatibility")
    ap.add_argument("--unlock-all", action="store_true", help="debug: unlock every stage and skin")
    args = ap.parse_args()

    pygame.init()
    pygame.display.set_caption("LokiMan2")
    screen = pygame.display.set_mode((W, H))
    clock = pygame.time.Clock()
    game = Game(screen, args.stage, args.scene)
    if args.unlock_all:
        game.save.update(cleared=True, expert_cleared=True, max_stage=4)
    while game.running:
        for e in pygame.event.get():
            game.handle_event(e)
        game.read_held()
        game.update()
        game.draw()
        clock.tick(FPS)
    pygame.quit()


if __name__ == "__main__":
    main()
