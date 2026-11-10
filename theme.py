"""
SIGNATURE THEME: Black & Gold Marble
====================================
Drop this file into any Streamlit tool and add two lines at the top of your app:

    import theme
    theme.apply_theme("My Tool", "A one-line tagline")

Everything else (marble background, gold buttons, cards, tabs, scrollbars) is automatic.
It also writes .streamlit/config.toml (if missing) so native widgets pick up the palette.

Brand palette (use these hex values in ANY project, Streamlit or not):
    Black      #0A0A0B   (dominant)      Gold        #F5C518   (primary accent)
    Gold bright #FFD60A  (highlights)    Gold deep   #C98F00   (depth / gradients)
    Gold soft  #FFE680   (light accent)  Ivory       #F4F1E8   (text)
    Muted      #9B9B9B   (secondary)     Warn #FF9F1C / Alert #FF4D4D (status only)

Reusable components: hero_html, donut_html, share_rows_html, progress_html.
"""

import html
import re
from pathlib import Path
from urllib.parse import quote

GOLD = "#F5C518"
GOLD_BRIGHT = "#FFD60A"
GOLD_SOFT = "#FFE680"
GOLD_DEEP = "#C98F00"
BLACK = "#0A0A0B"
IVORY = "#F4F1E8"
MUTED = "#9B9B9B"
WARN = "#FF9F1C"
ALERT = "#FF4D4D"

# Slice colours: gold family plus ivory/greys, ordered so neighbours contrast.
PALETTE = [
    "#FFD60A", "#E8E3D3", "#FF9F1C", "#8E8E8E", "#C98F00", "#FFF3B0",
    "#7A6A2E", "#BFBFBF", "#FFB703", "#5E5E5E", "#F0D98A", "#A67C00",
]

CONFIG_TOML = """[theme]
base = "dark"
primaryColor = "#F5C518"
backgroundColor = "#0A0A0B"
secondaryBackgroundColor = "#141416"
textColor = "#F4F1E8"
font = "sans serif"
"""


# ---------------------------------------------------------------- marble background

def _ridge(width, n=41):
    """Alpha lookup table that is 1 at noise value 0.5 and falls to 0 `width` steps away.
    Applying it to smooth noise draws continuous contour lines, which read as marble veins."""
    mid = (n - 1) / 2
    return " ".join(f"{max(0.0, 1 - abs(i - mid) / width):.2f}" for i in range(n))


def marble_svg(width=(1.0, 0.8), warp=(120, 90), opacity=(0.5, 0.18)):
    """Procedural black marble: soft grey clouds + continuous, warped gold and ivory veins.

    width   = vein thickness (gold, ivory); bigger = thicker veins
    warp    = how much the veins swirl
    opacity = vein strength (gold, ivory)
    """
    region = "filterUnits='userSpaceOnUse' x='-300' y='-300' width='2200' height='1600' color-interpolation-filters='sRGB'"

    def vein(fid, freq, seed, wseed, w, wp, rgb):
        return (
            f"<filter id='{fid}' {region}>"
            f"<feTurbulence type='fractalNoise' baseFrequency='{freq}' numOctaves='4' seed='{seed}' result='t'/>"
            f"<feColorMatrix in='t' type='matrix' values='0 0 0 0 {rgb[0]}  0 0 0 0 {rgb[1]}  0 0 0 0 {rgb[2]}  1 0 0 0 0' result='a'/>"
            f"<feComponentTransfer in='a' result='r'><feFuncA type='table' tableValues='{_ridge(w)}'/></feComponentTransfer>"
            f"<feTurbulence type='fractalNoise' baseFrequency='0.0032' numOctaves='2' seed='{wseed}' result='w'/>"
            f"<feDisplacementMap in='r' in2='w' scale='{wp}' xChannelSelector='R' yChannelSelector='G' result='d'/>"
            "<feGaussianBlur in='d' stdDeviation='0.6'/></filter>"
        )

    return (
        "<svg xmlns='http://www.w3.org/2000/svg' width='1600' height='1000' viewBox='0 0 1600 1000' "
        "preserveAspectRatio='xMidYMid slice'><defs>"
        f"<filter id='c' {region}>"
        "<feTurbulence type='fractalNoise' baseFrequency='0.0022 0.0038' numOctaves='5' seed='21'/>"
        "<feColorMatrix type='matrix' values='0 0 0 0 .21  0 0 0 0 .21  0 0 0 0 .22  0 0 0 1.5 -.64'/>"
        "</filter>"
        + vein("g", "0.0030 0.0058", 8, 3, width[0], warp[0], (".96", ".77", ".10"))
        + vein("i", "0.0042 0.0080", 14, 9, width[1], warp[1], (".95", ".93", ".87"))
        + "</defs>"
        f"<rect width='100%' height='100%' fill='{BLACK}'/>"
        "<g><rect x='-300' y='-300' width='2200' height='1600' filter='url(#c)'/>"
        f"<rect x='-300' y='-300' width='2200' height='1600' filter='url(#g)' opacity='{opacity[0]}'/>"
        f"<rect x='-300' y='-300' width='2200' height='1600' filter='url(#i)' opacity='{opacity[1]}'/></g>"
        "</svg>"
    )


def marble_data_uri():
    return "data:image/svg+xml," + quote(marble_svg(), safe="")


# ---------------------------------------------------------------- CSS

_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Playfair+Display:wght@600;700;800&display=swap');
:root{--gold:#F5C518;--gold-bright:#FFD60A;--gold-soft:#FFE680;--gold-deep:#C98F00;--black:#0A0A0B;
--ivory:#F4F1E8;--muted:#9B9B9B;--line:rgba(245,197,24,.28);--panel:rgba(10,10,11,.76);}
.stApp{background-color:var(--black);background-image:url("__MARBLE__");background-size:cover;
background-position:center;background-attachment:fixed;color:var(--ivory);font-family:'Inter',system-ui,sans-serif;}
[data-testid="stHeader"]{background:transparent;}
.block-container{padding-top:2rem;max-width:1250px;}
h1,h2,h3{font-family:'Playfair Display',Georgia,serif!important;letter-spacing:.2px;}
h2,h3{color:var(--gold)!important;}
p,label,span,li{color:var(--ivory);}
[data-testid="stCaptionContainer"],small{color:var(--muted)!important;}

/* hero */
.hero{padding:6px 0 18px;margin-bottom:10px;border-bottom:1px solid transparent;
border-image:linear-gradient(90deg,var(--gold),rgba(245,197,24,0)) 1;}
.hero-kicker{font-size:.72rem;letter-spacing:.42em;text-transform:uppercase;color:var(--gold-deep);font-weight:600;}
.hero-title{font-family:'Playfair Display',Georgia,serif;font-weight:800;font-size:3.1rem;line-height:1.1;margin:4px 0 2px;
background:linear-gradient(90deg,#FFE680,#F5C518 45%,#C98F00);-webkit-background-clip:text;background-clip:text;
color:transparent;text-shadow:0 0 40px rgba(245,197,24,.18);}
.hero-tag{color:#cfcfcf;font-size:1.02rem;margin:0;}

/* sidebar */
[data-testid="stSidebar"]{background:linear-gradient(180deg,rgba(7,7,8,.97),rgba(14,14,16,.97));
border-right:1px solid var(--line);}

/* metric cards */
[data-testid="stMetric"]{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;
box-shadow:0 10px 30px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.05);backdrop-filter:blur(6px);}
[data-testid="stMetricValue"]{color:var(--gold-bright)!important;font-weight:700;}
[data-testid="stMetricLabel"] *{color:#bdbdbd!important;letter-spacing:.04em;}

/* buttons */
.stButton>button,.stDownloadButton>button,[data-testid="stFormSubmitButton"] button{
background:linear-gradient(135deg,#FFD60A,#E6A800)!important;color:#0A0A0B!important;font-weight:700;border:none!important;
border-radius:10px;padding:.55rem 1.1rem;box-shadow:0 4px 14px rgba(0,0,0,.45);transition:all .18s ease;}
.stButton>button:hover,.stDownloadButton>button:hover,[data-testid="stFormSubmitButton"] button:hover{
box-shadow:0 0 24px rgba(245,197,24,.5);transform:translateY(-1px);filter:brightness(1.06);}
.stButton>button p,.stDownloadButton>button p,[data-testid="stFormSubmitButton"] button p{color:#0A0A0B!important;font-weight:700;}

/* tabs */
[data-baseweb="tab-list"]{gap:8px;border-bottom:1px solid var(--line);}
[data-baseweb="tab"]{color:#bdbdbd;font-weight:600;}
[data-baseweb="tab"][aria-selected="true"]{color:var(--gold)!important;}
[data-baseweb="tab-highlight"]{background-color:var(--gold)!important;}

/* inputs, uploader, alerts, tables */
.stTextInput input,.stNumberInput input,.stTextArea textarea{background:rgba(12,12,13,.88)!important;color:var(--ivory)!important;
border:1px solid rgba(245,197,24,.2)!important;border-radius:10px!important;}
.stTextInput input:focus,.stNumberInput input:focus{border-color:var(--gold)!important;box-shadow:0 0 0 1px var(--gold)!important;}
[data-baseweb="select"]>div{background:rgba(12,12,13,.88)!important;border:1px solid rgba(245,197,24,.2)!important;border-radius:10px!important;}
[data-testid="stFileUploader"] section{background:rgba(10,10,11,.65);border:1px dashed var(--line);border-radius:12px;}
[data-testid="stForm"]{background:var(--panel);border:1px solid var(--line);border-radius:16px;backdrop-filter:blur(6px);}
[data-testid="stAlert"]{background:rgba(18,18,20,.82)!important;border:1px solid var(--line);border-radius:12px;}
[data-testid="stExpander"]{background:rgba(10,10,11,.62);border:1px solid rgba(245,197,24,.22);border-radius:12px;}
[data-testid="stDataFrame"]{border:1px solid var(--line);border-radius:12px;overflow:hidden;}
::-webkit-scrollbar{width:10px;height:10px;}::-webkit-scrollbar-track{background:#0A0A0B;}
::-webkit-scrollbar-thumb{background:linear-gradient(#C98F00,#F5C518);border-radius:8px;}

/* components */
.gold-card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px 22px;
box-shadow:0 12px 36px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.05);backdrop-filter:blur(6px);}
.donut-wrap{display:flex;justify-content:center;align-items:center;padding:6px 0;}
.donut{border-radius:50%;display:flex;align-items:center;justify-content:center;
box-shadow:0 0 46px rgba(245,197,24,.2),0 0 0 6px rgba(10,10,11,.85),0 0 0 7px rgba(245,197,24,.4);}
.donut-hole{border-radius:50%;background:radial-gradient(circle at 30% 25%,#232326,#0A0A0B 70%);
border:1px solid rgba(245,197,24,.35);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;}
.donut-top{font-size:.68rem;letter-spacing:.26em;text-transform:uppercase;color:var(--muted);}
.donut-val{font-family:'Playfair Display',Georgia,serif;font-size:1.45rem;font-weight:700;color:var(--gold-bright);margin-top:2px;}
.share-list{display:flex;flex-direction:column;gap:13px;}
.share-head{display:flex;align-items:center;gap:10px;font-size:.93rem;}
.dot{width:11px;height:11px;border-radius:50%;flex:none;box-shadow:0 0 8px rgba(245,197,24,.35);}
.share-name{flex:1;color:var(--ivory);font-weight:500;}
.share-amt{color:var(--muted);font-variant-numeric:tabular-nums;}
.share-pct{min-width:58px;text-align:right;color:var(--gold-bright);font-weight:700;font-variant-numeric:tabular-nums;}
.share-track,.gbar{height:7px;background:rgba(255,255,255,.07);border-radius:6px;overflow:hidden;margin-top:6px;}
.share-fill{height:100%;border-radius:6px;}
.gbar-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#C98F00,#FFD60A);}
.gbar-fill.watch{background:linear-gradient(90deg,#d97706,#FF9F1C);}
.gbar-fill.over{background:linear-gradient(90deg,#c62828,#FF4D4D);}
"""


def build_css():
    css = _CSS.replace("__MARBLE__", marble_data_uri())
    return re.sub(r"\s+", " ", css)  # single line: Streamlit's markdown parser dislikes blank lines


# ---------------------------------------------------------------- components (return HTML strings)

def _colors(n):
    return [PALETTE[i % len(PALETTE)] for i in range(n)]


def hero_html(title, tagline="", kicker=""):
    k = f"<div class='hero-kicker'>{html.escape(kicker)}</div>" if kicker else ""
    t = f"<p class='hero-tag'>{html.escape(tagline)}</p>" if tagline else ""
    return f"<div class='hero'>{k}<div class='hero-title'>{html.escape(title)}</div>{t}</div>"


def donut_html(items, center_top="", center_value="", size=240):
    """items: list of (label, value). Pure-CSS donut; no chart library needed."""
    total = sum(v for _, v in items) or 1
    stops, acc = [], 0.0
    for (_, v), col in zip(items, _colors(len(items))):
        start = acc / total * 100
        acc += v
        stops.append(f"{col} {start:.3f}% {acc / total * 100:.3f}%")
    hole = int(size * 0.62)
    return (
        f"<div class='donut-wrap'><div class='donut' style='width:{size}px;height:{size}px;"
        f"background:conic-gradient({', '.join(stops)});'>"
        f"<div class='donut-hole' style='width:{hole}px;height:{hole}px;'>"
        f"<div class='donut-top'>{html.escape(center_top)}</div>"
        f"<div class='donut-val'>{html.escape(center_value)}</div></div></div></div>"
    )


def share_rows_html(items, fmt=lambda v: f"{v:,.0f}"):
    """Ranked rows with colour dot, amount, percentage and a share bar. items: (label, value)."""
    total = sum(v for _, v in items) or 1
    rows = []
    for (label, v), col in zip(items, _colors(len(items))):
        pct = v / total * 100
        rows.append(
            f"<div><div class='share-head'><span class='dot' style='background:{col}'></span>"
            f"<span class='share-name'>{html.escape(str(label))}</span>"
            f"<span class='share-amt'>{html.escape(fmt(v))}</span>"
            f"<span class='share-pct'>{pct:.1f}%</span></div>"
            f"<div class='share-track'><div class='share-fill' style='width:{pct:.2f}%;background:{col}'></div></div></div>"
        )
    return "<div class='share-list'>" + "".join(rows) + "</div>"


def progress_html(fraction, state="ok"):
    """Themed progress bar. state: 'ok' (gold), 'watch' (amber) or 'over' (red)."""
    width = min(max(fraction, 0.0), 1.0) * 100
    return f"<div class='gbar'><div class='gbar-fill {state}' style='width:{width:.1f}%'></div></div>"


def debt_card_html(name, amount_text, fraction, caption="", state="ok"):
    """A compact card: name + amount, a progress bar and a small caption."""
    return (
        "<div class='gold-card' style='margin-bottom:12px;padding:14px 18px'>"
        f"<div class='share-head'><span class='share-name' style='font-weight:600'>{html.escape(name)}</span>"
        f"<span class='share-pct' style='min-width:0'>{html.escape(amount_text)}</span></div>"
        f"{progress_html(fraction, state)}"
        f"<div style='color:#9B9B9B;font-size:.8rem;margin-top:8px'>{html.escape(caption)}</div></div>"
    )


def note_card_html(title, detail="", badge=""):
    """A compact card with a title, optional badge on the right and a small detail line."""
    return (
        "<div class='gold-card' style='margin-bottom:8px;padding:14px 18px'>"
        f"<div class='share-head'><span class='share-name' style='font-weight:600'>{html.escape(title)}</span>"
        f"<span class='share-pct' style='min-width:0'>{html.escape(badge)}</span></div>"
        f"<div style='color:#9B9B9B;font-size:.82rem;margin-top:6px'>{html.escape(detail)}</div></div>"
    )


# ---------------------------------------------------------------- Streamlit entry point

def ensure_streamlit_config():
    """Create .streamlit/config.toml next to where the app is run, if it doesn't exist."""
    try:
        path = Path.cwd() / ".streamlit" / "config.toml"
        if not path.exists():
            path.parent.mkdir(exist_ok=True)
            path.write_text(CONFIG_TOML)
    except OSError:
        pass  # purely cosmetic; the CSS carries the look either way


def apply_theme(title, tagline="", icon="✦", kicker="Black & Gold"):
    """Call first in your app. Sets the page, injects the theme and draws the hero header."""
    import streamlit as st

    ensure_streamlit_config()
    st.set_page_config(page_title=title, page_icon=icon, layout="wide")
    st.markdown(f"<style>{build_css()}</style>", unsafe_allow_html=True)
    st.markdown(hero_html(title, tagline, kicker), unsafe_allow_html=True)
