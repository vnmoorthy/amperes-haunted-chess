"""Add motion to the deck written by build.js: a fade transition on every slide, and staggered fade-in
entrance animations that start on their own when the slide opens.

Shapes whose name starts with "aN " (set through objectName in build.js) fade in as group N, groups
0.25 s apart, each fade 0.5 s. Nothing needs a click, so a presenter can advance at any time.

Usage: python3 postprocess.py Amperes_Failure_Lab.pptx
"""
import re
import sys
import zipfile
from pathlib import Path

TRANSITION = '<p:transition spd="med"><p:fade/></p:transition>'
STAGGER_MS, FADE_MS = 250, 500


def effect(ids, spid, delay, first, has_text):
    i0, i1, i2 = next(ids), next(ids), next(ids)
    node = "afterEffect" if first else "withEffect"
    grp = ' grpId="0"' if has_text else ""
    return (f'<p:par><p:cTn id="{i0}" presetID="10" presetClass="entr" presetSubtype="0" fill="hold"{grp} nodeType="{node}">'
            f'<p:stCondLst><p:cond delay="{delay}"/></p:stCondLst><p:childTnLst>'
            f'<p:set><p:cBhvr><p:cTn id="{i1}" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>'
            f'<p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl><p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst>'
            f'</p:cBhvr><p:to><p:strVal val="visible"/></p:to></p:set>'
            f'<p:animEffect transition="in" filter="fade"><p:cBhvr><p:cTn id="{i2}" dur="{FADE_MS}"/>'
            f'<p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl></p:cBhvr></p:animEffect>'
            f'</p:childTnLst></p:cTn></p:par>')


def timing(shapes):
    """shapes: list of (group, spid, has_text) in document order."""
    def counter():
        n = 5
        while True:
            yield n
            n += 1
    ids = counter()
    effects = []
    for k, (group, spid, has_text) in enumerate(sorted(shapes, key=lambda t: t[0])):
        effects.append(effect(ids, spid, (group - 1) * STAGGER_MS, k == 0, has_text))
    bld = "".join(f'<p:bldP spid="{spid}" grpId="0" animBg="1"/>' for _, spid, has_text in shapes if has_text)
    return ('<p:timing><p:tnLst><p:par><p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
            '<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst>'
            '<p:par><p:cTn id="3" fill="hold"><p:stCondLst><p:cond delay="indefinite"/>'
            '<p:cond evt="onBegin" delay="0"><p:tn val="2"/></p:cond></p:stCondLst><p:childTnLst>'
            '<p:par><p:cTn id="4" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
            + "".join(effects) +
            '</p:childTnLst></p:cTn></p:par>'
            '</p:childTnLst></p:cTn></p:par>'
            '</p:childTnLst></p:cTn>'
            '<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>'
            '<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst>'
            '</p:seq></p:childTnLst></p:cTn></p:par></p:tnLst>'
            + (f"<p:bldLst>{bld}</p:bldLst>" if bld else "") + '</p:timing>')


ELEM = re.compile(r'<p:(sp|pic|cxnSp)>.*?</p:\1>', re.S)
CNV = re.compile(r'<p:cNvPr id="(\d+)" name="a(\d+) [^"]*"')


def animate(xml):
    shapes = []
    for m in ELEM.finditer(xml):
        el = m.group(0)
        c = CNV.search(el)
        if not c:
            continue
        has_text = m.group(1) == "sp" and "<p:txBody>" in el and "<a:t>" in el
        shapes.append((int(c.group(2)), c.group(1), has_text))
    return shapes


def process(xml):
    xml = re.sub(r'<p:transition\b.*?</p:transition>|<p:transition\b[^>]*/>', "", xml, flags=re.S)
    xml = re.sub(r'<p:timing>.*?</p:timing>', "", xml, flags=re.S)
    shapes = animate(xml)
    extra = TRANSITION + (timing(shapes) if shapes else "")
    if "</p:clrMapOvr>" in xml:
        xml = xml.replace("</p:clrMapOvr>", "</p:clrMapOvr>" + extra, 1)
    elif "<p:extLst>" in xml.split("</p:cSld>", 1)[1]:
        head, tail = xml.split("</p:cSld>", 1)
        xml = head + "</p:cSld>" + tail.replace("<p:extLst>", extra + "<p:extLst>", 1)
    else:
        xml = xml.replace("</p:sld>", extra + "</p:sld>", 1)
    return xml, len(shapes)


def main(path):
    path = Path(path)
    tmp = path.with_suffix(".tmp.pptx")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", item.filename):
                xml, n = process(data.decode("utf-8"))
                data = xml.encode("utf-8")
                print(f"{item.filename}: fade transition, {n} animated shapes")
            zout.writestr(item, data)
    tmp.replace(path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("Amperes_Failure_Lab.pptx"))
