"""Regenerates the README images:  python docs/make_images.py"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.xkcd()   # hand-drawn look
from matplotlib.patches import FancyBboxPatch

BG, FG, ACC, UP, DN = "#fffdf5", "#222222", "#2563eb", "#16a34a", "#dc2626"


def box(ax, x, y, w, h, text, color=ACC, fs=10):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.15",
                                fc="#fff3c4", ec=color, lw=2))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", color=FG, fontsize=fs, fontweight="bold")


def canvas(w, h):
    fig, ax = plt.subplots(figsize=(w, h), facecolor=BG)
    ax.set_facecolor(BG); ax.axis("off")
    return fig, ax


# 1. pipeline
fig, ax = canvas(12, 4.6)
ax.set_xlim(0, 12); ax.set_ylim(0, 4.6)
ax.text(6, 4.3, "5 stages vote on every closed 15m candle", ha="center", color=FG, fontsize=15, fontweight="bold")
names = ["1 Trend\nHH+HL / LH+LL", "2 Break of\nStructure", "3 EMA\n20/50/200", "4 Continuation\n/ Reversal", "5 Funding\n(contrarian)"]
for i, n in enumerate(names):
    box(ax, 0.2 + i * 2.35, 2.5, 2.1, 1.1, n)
    ax.annotate("", xy=(1.25 + i * 2.35, 2.0), xytext=(1.25 + i * 2.35, 2.45), arrowprops=dict(arrowstyle="->", color=FG))
box(ax, 3.2, 1.0, 5.6, 0.95, "count votes: LONG vs SHORT", color="#9333ea", fs=12)
ax.annotate("", xy=(6, 0.75), xytext=(6, 0.98), arrowprops=dict(arrowstyle="->", color=FG))
box(ax, 0.6, -0.05, 4.7, 0.8, ">= 2 agree and outvote  ->  TRADE", color=UP, fs=11)
box(ax, 6.7, -0.05, 4.7, 0.8, "otherwise  ->  no trade", color=DN, fs=11)
ax.set_ylim(-0.2, 4.6)
fig.savefig("docs/pipeline.png", dpi=150, facecolor=BG, bbox_inches="tight"); plt.close(fig)

# 2. vote examples
fig, ax = canvas(10, 4.2)
ax.set_xlim(0, 10); ax.set_ylim(0, 5)
ax.text(5, 4.65, "Vote examples (minimum = 2)", ha="center", color=FG, fontsize=15, fontweight="bold")
cols = ["Trend", "BoS", "EMA", "Signal", "Funding", "Result"]
rows = [([1, 1, 0, 0, 0], "LONG: 2 vs 0"), ([1, 1, 1, 1, 0], "LONG: 4 vs 0"),
        ([-1, 1, 0, 1, 0], "LONG: 2 vs 1"), ([1, 0, 0, 0, -1], "NO TRADE: 1 vs 1"),
        ([0, 0, 0, 1, 0], "NO TRADE: 1 vs 0")]
sym = {1: ("L", UP), -1: ("S", DN), 0: ("-", "#888888")}
for j, c in enumerate(cols):
    ax.text(1.2 + j * 1.45, 4.05, c, ha="center", color=ACC, fontsize=11, fontweight="bold")
for i, (v, res) in enumerate(rows):
    y = 3.3 - i * 0.72
    for j, x in enumerate(v):
        t, c = sym[x]
        ax.text(1.2 + j * 1.45, y, t, ha="center", va="center", color=c, fontsize=16, fontweight="bold")
    ax.text(1.2 + 5 * 1.45, y, res, ha="center", va="center", color=UP if "NO" not in res else DN, fontsize=10, fontweight="bold")
ax.text(5, 0.05, "L = votes long   S = votes short   - = abstains", ha="center", color="#666666", fontsize=9)
fig.savefig("docs/votes.png", dpi=150, facecolor=BG, bbox_inches="tight"); plt.close(fig)

# 3. safety layers
fig, ax = canvas(10, 4)
ax.set_xlim(0, 10); ax.set_ylim(0, 4)
ax.text(5, 3.7, "A signal must pass every risk gate", ha="center", color=FG, fontsize=15, fontweight="bold")
g = ["Price drift\n<= 0.3%", "Daily loss\n> -15%", "Stop width\n<= 3%", "Liq. >= 2x\nstop", "Size >= 1 lot\nmargin <= 100%"]
for i, t in enumerate(g):
    box(ax, 0.15 + i * 1.95, 1.7, 1.75, 1.2, t, fs=9)
    if i < 4:
        ax.annotate("", xy=(1.95 + i * 1.95 + 0.12, 2.3), xytext=(1.9 + i * 1.95, 2.3), arrowprops=dict(arrowstyle="->", color=FG))
box(ax, 2.5, 0.2, 5, 0.9, "ENTER with exchange stop + take-profit", color=UP, fs=11)
ax.annotate("", xy=(5, 1.15), xytext=(5, 1.65), arrowprops=dict(arrowstyle="->", color=FG))
fig.savefig("docs/risk_gates.png", dpi=150, facecolor=BG, bbox_inches="tight"); plt.close(fig)
