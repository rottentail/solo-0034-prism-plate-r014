#!/usr/bin/env python3
"""Independent numeric cross-check for transformable radial gradients.

This script deliberately re-derives the renderer instead of reusing the Go
code paths:

* radial parameter t is solved from the raw quadratic
  a*t^2 + b*t + c = 0 (t = (-b + sqrt(b^2-4ac)) / 2a), whereas the service
  uses the algebraically equivalent but numerically different rationalized
  root (f.d + sqrt((f.d)^2 + |d|^2(1-|f|^2))) / (1-|f|^2);
* PNG bytes are decoded with a self-contained stdlib zlib/struct decoder
  instead of any image library;
* sRGB transfer, premultiplied compositing and 2x2 supersampling are
  reimplemented from the documented scene contract.

It builds the binary, drives real HTTP against an ephemeral persistence root,
restarts the service and round-trips a bundle, so persistence and admission
are exercised end to end.
"""
import copy, hashlib, json, math, os, pathlib, signal, socket, struct, subprocess, sys, tempfile, time, urllib.error, urllib.request, zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Independent PNG decoder (8-bit, RGBA palette-free output from image/png).
# --------------------------------------------------------------------------
def read_png(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    pos, width, height, bit_depth, color_type, interlace = 8, 0, 0, 0, 0, 0
    idat = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if bit_depth != 8 or interlace != 0 or color_type not in (0, 2, 6):
        raise ValueError("unsupported png layout")
    channels = {0: 1, 2: 3, 6: 4}[color_type]
    bpp, stride = channels, width * channels
    raw = zlib.decompress(idat)
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(height):
        f = raw[i]
        i += 1
        line = bytearray(raw[i:i + stride])
        i += stride
        for x in range(stride):
            left = line[x - bpp] if x >= bpp else 0
            up = prev[x]
            ul = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + left) & 255
            elif f == 2:
                line[x] = (line[x] + up) & 255
            elif f == 3:
                line[x] = (line[x] + ((left + up) >> 1)) & 255
            elif f == 4:
                p = left + up - ul
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - ul)
                pr = left if pa <= pb and pa <= pc else (up if pb <= pc else ul)
                line[x] = (line[x] + pr) & 255
            elif f != 0:
                raise ValueError("bad filter")
        rows.append(bytes(line))
        prev = line
    rgba = []
    for line in rows:
        out = bytearray(width * 4)
        for x in range(width):
            v = line[x * channels:(x + 1) * channels]
            if color_type == 0:
                out[x * 4:x * 4 + 3] = v * 3
                out[x * 4 + 3] = 255
            elif color_type == 2:
                out[x * 4:x * 4 + 3] = v
                out[x * 4 + 3] = 255
            else:
                out[x * 4:x * 4 + 4] = v
        rgba.append(bytes(out))
    return width, height, rgba


# --------------------------------------------------------------------------
# Independent re-implementation of the documented rendering contract.
# --------------------------------------------------------------------------
def srgb_to_linear(v):
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def linear_to_srgb(v):
    v = min(1.0, max(0.0, v))
    return v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


def as_tuple(color):
    if isinstance(color, dict):
        return color["r"], color["g"], color["b"], color["a"]
    return tuple(color)


def premul(color):
    r, g, b, a = as_tuple(color)
    return (srgb_to_linear(r) * a, srgb_to_linear(g) * a, srgb_to_linear(b) * a, a)


def over(src, dst):
    # premultiplied source-over: a fully transparent source contributes no
    # color at all, which is what makes an alpha-zero outer stop fade out
    # instead of darkening to a black ring.
    a = src[3]
    if a <= 0.0:
        return dst
    out_a = a + dst[3] * (1 - a)
    return tuple(s + d * (1 - a) for s, d in zip(src[:3], dst[:3])) + (out_a,)


def radial_t(brush, x, y):
    """SVG ray parameter via the explicit circle-intersection factor.

    Position along the ray is f + s*(u-f); the sample point u sits at s=1 and
    the unit-circle crossing at s=S, solved here as the positive root of the
    raw quadratic. The stop parameter is t = 1/S (clamped). The service reaches
    the same value through a different, post-solve reciprocal evaluation rather
    than this textbook quadratic formula.
    """
    cx, cy = brush["center"]["x"], brush["center"]["y"]
    rx, ry = brush["radius_x"], brush["radius_y"]
    ux, uy = (x - cx) / rx, (y - cy) / ry
    gx, gy = brush["focus"]["x"] / rx, brush["focus"]["y"] / ry
    dx, dy = ux - gx, uy - gy
    a = dx * dx + dy * dy
    if a == 0.0:
        return 0.0  # sample at the focal point: first stop by definition
    b = 2 * (gx * dx + gy * dy)
    c = gx * gx + gy * gy - 1.0
    disc = b * b - 4 * a * c  # always >= 0 because |f|^2 < 1
    s_factor = (-b + math.sqrt(max(0.0, disc))) / (2 * a)
    if s_factor <= 0.0:
        return 0.0
    return min(1.0, max(0.0, 1.0 / s_factor))


def sample_brush(brush, x, y):
    t = min(1.0, max(0.0, radial_t(brush, x, y)))
    stops = brush["stops"]
    for i in range(1, len(stops)):
        lo, hi = stops[i - 1], stops[i]
        if t <= hi["at"]:
            u = (t - lo["at"]) / (hi["at"] - lo["at"])
            p0, p1 = premul(lo["color"]), premul(hi["color"])
            return tuple(p0[k] + (p1[k] - p0[k]) * u for k in range(4))
    return premul(stops[-1]["color"])


def expected_pixel(doc, px, py):
    bg = premul(doc["background"])
    acc = [0.0, 0.0, 0.0, 0.0]
    for sy in range(2):
        for sx in range(2):
            x = px + (sx + 0.5) / 2
            y = py + (sy + 0.5) / 2
            dst = bg
            for layer in doc["layers"]:
                src = (0.0, 0.0, 0.0, 0.0)
                for shape in layer["shapes"]:
                    m = shape["transform"]
                    det = m[0] * m[3] - m[1] * m[2]
                    qx = (m[3] * (x - m[4]) - m[2] * (y - m[5])) / det
                    qy = (-m[1] * (x - m[4]) + m[0] * (y - m[5])) / det
                    if rect_contains(shape["path"], qx, qy):
                        src = over(sample_brush(shape["brush"], qx, qy), src)
                scaled = tuple(v * layer["opacity"] for v in src)
                if layer["blend"] == "normal":
                    dst = over(scaled, dst)
                else:
                    raise AssertionError("controlled plate must stay normal blend")
            for k in range(4):
                acc[k] += dst[k] / 4
    out = []
    for k in range(3):
        out.append(int(math.floor(linear_to_srgb(acc[k] / acc[3]) * 255 + 0.5)))
    out.append(int(math.floor(min(1.0, acc[3]) * 255 + 0.5)))
    return tuple(out)


def rect_contains(path, x, y):
    rings, ring = [], []
    for op in path:
        if op["kind"] == "M":
            ring = [op["points"][0]]
        elif op["kind"] == "L":
            ring.append(op["points"][0])
        elif op["kind"] == "Z":
            rings.append(ring)
            ring = []
    inside = False
    for pts in rings:
        hit = False
        for i, (a) in enumerate(pts):
            ax, ay = a["x"], a["y"]
            b = pts[(i + 1) % len(pts)]
            bx, by = b["x"], b["y"]
            if (ay <= y < by) or (by <= y < ay):
                if ax + (y - ay) * (bx - ax) / (by - ay) > x:
                    hit = not hit
        inside = inside != hit
    return inside


def rect_path(x0, y0, x1, y1):
    return [
        {"kind": "M", "points": [{"x": x0, "y": y0}]},
        {"kind": "L", "points": [{"x": x1, "y": y0}]},
        {"kind": "L", "points": [{"x": x1, "y": y1}]},
        {"kind": "L", "points": [{"x": x0, "y": y1}]},
        {"kind": "Z"},
    ]


# --------------------------------------------------------------------------
# HTTP journey.
# --------------------------------------------------------------------------
class Client:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, data=None, status=200):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=35) as r:
                payload = r.read()
                code = r.status
        except urllib.error.HTTPError as e:
            payload, code = e.read(), e.code
        if code != status:
            raise AssertionError(f"{method} {path}: expected {status}, got {code}: {payload[:200]!r}")
        return json.loads(payload) if payload else None

    def raw(self, path):
        with urllib.request.urlopen(self.base + path, timeout=35) as r:
            return r.read()

    def render(self, plate, fmt, revision=1):
        return self.call("POST", f"/v1/plates/{plate}/render?revision={revision}&format={fmt}", status=201)


def controlled_document(plate_id, transform, focus, rx=30.0, ry=30.0):
    brush = {
        "kind": "radial",
        "center": {"x": 40, "y": 40},
        "radius_x": rx, "radius_y": ry,
        "focus": focus,
        "color": {"r": 0, "g": 0, "b": 0, "a": 0},
        "stops": [
            {"at": 0, "color": {"r": 1.0, "g": 0.9, "b": 0.7, "a": 0.8}},
            {"at": 0.5, "color": {"r": 0.9, "g": 0.3, "b": 0.1, "a": 0.4}},
            {"at": 1, "color": {"r": 0.9, "g": 0.3, "b": 0.1, "a": 0.0}},
        ],
    }
    return {
        "id": plate_id, "title": "数值对照板",
        "document": {
            "width": 80, "height": 80,
            "background": {"r": 0.05, "g": 0.07, "b": 0.11, "a": 1},
            "layers": [{
                "id": "only-sheet", "opacity": 1, "blend": "normal",
                "shapes": [{
                    "id": "only-shape", "fill_rule": "nonzero",
                    "path": rect_path(-200, -200, 200, 200),
                    "transform": transform, "brush": brush,
                }],
            }],
        },
    }


def main():
    with tempfile.TemporaryDirectory(prefix="prism-radial-") as tmp:
        base = pathlib.Path(tmp)
        binary = base / "prismplate"
        subprocess.run(["go", "build", "-o", str(binary), "./cmd/prismplate"], cwd=ROOT, check=True)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        url = f"http://127.0.0.1:{port}"
        log = (base / "service.log").open("w")
        data_root = base / "state"
        proc = None

        def stop():
            nonlocal proc
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=45)
            proc = None

        def start(root=None):
            nonlocal proc
            target = root or data_root
            last = None
            for attempt in range(20):
                proc = subprocess.Popen([str(binary), "-listen", f"127.0.0.1:{port}",
                                         "-data", str(target)],
                                        stdout=log, stderr=log)
                ready = False
                for _ in range(40):
                    if proc.poll() is not None:
                        last = open(log.name).read()
                        time.sleep(0.15)  # let the old listener release the port
                        break
                    try:
                        urllib.request.urlopen(url + "/healthz", timeout=1).close()
                        ready = True
                        break
                    except OSError:
                        time.sleep(0.05)
                if ready:
                    return
            raise RuntimeError("startup failed: " + str(last))

        try:
            start()
            api = Client(url)

            # --- controlled identity plate ----------------------------------
            focus = {"x": 10, "y": -8}
            plate = controlled_document("check-plate", [1, 0, 0, 1, 0, 0], focus)
            api.call("POST", "/v1/plates", plate, status=201)
            doc = plate["document"]
            brush = doc["layers"][0]["shapes"][0]["brush"]
            art = api.render("check-plate", "png")
            png = api.raw("/v1/artifacts/" + art["id"])
            w, h, rows = read_png(png)
            assert (w, h) == (80, 80)

            # 1. Pixel-level cross-check against the independent formula.
            probes = [(5, 5), (12, 60), (40, 40), (50, 32), (63, 47), (70, 40), (75, 12), (33, 55)]
            for px, py in probes:
                got = tuple(rows[py][px * 4 + k] for k in range(4))
                want = expected_pixel(doc, px, py)
                if any(abs(g - q) > 2 for g, q in zip(got, want)):
                    raise AssertionError(f"pixel mismatch at ({px},{py}): go={got} python={want}")

            # 2. No dark ring. The ending ellipse is centered at (40,40) with
            #    radii 30, so the points below are strictly outside it: the
            #    transparent last stop must leave the exact backdrop pixel,
            #    including its RGB channels, with no black or colored halo.
            bg_doc = {"background": doc["background"], "layers": []}
            bg8 = expected_pixel(bg_doc, 0, 0)
            exterior = [(2, 2), (78, 2), (2, 78), (78, 78), (4, 40), (76, 40), (40, 4), (40, 76)]
            for px, py in exterior:
                # confirm the probe really is outside the normalized unit circle
                nx, ny = (px - 40) / 30, (py - 40) / 30
                assert nx * nx + ny * ny > 1.0, (px, py, nx * nx + ny * ny)
                got = tuple(rows[py][px * 4 + k] for k in range(4))
                if got != bg8:
                    raise AssertionError(f"dark ring at exterior ({px},{py}): {got} != backdrop {bg8}")

            # 2b. Across the boundary the alpha contribution must decay to zero
            # monotonically along a radial row; a premultiplied dark ring would
            # show up as a non-monotone RGB or an alpha floor outside.
            outside_alpha = [rows[40][px * 4 + 3] for px in range(72, 79)]
            assert all(a == bg8[3] for a in outside_alpha), outside_alpha

            # 3. Focus maps to the first stop (zero-length ray).
            t_focus = radial_t(brush, 50, 32)
            assert abs(t_focus) < 1e-12, t_focus
            focus_px = tuple(rows[32][50 * 4 + k] for k in range(4))
            want_focus = expected_pixel(doc, 50, 32)
            assert all(abs(a - b) <= 2 for a, b in zip(focus_px, want_focus))

            # 4. Stretched ellipse: non-uniform shape transform carries the
            #    local gradient. The same local t must appear at T(q).
            stretched = controlled_document(
                "stretch-plate", [2, 0, 0, 0.5, 40, 30], {"x": 8, "y": 6}, rx=20.0, ry=20.0)
            api.call("POST", "/v1/plates", stretched, status=201)
            sdoc = stretched["document"]
            sbrush = sdoc["layers"][0]["shapes"][0]["brush"]
            sart = api.render("stretch-plate", "png")
            _, _, srows = read_png(api.raw("/v1/artifacts/" + sart["id"]))
            m = sdoc["layers"][0]["shapes"][0]["transform"]
            for lx, ly in [(10, 0), (0, 10), (-12, 7), (15, -9)]:
                wx, wy = m[0] * lx + m[2] * ly + m[4], m[1] * lx + m[3] * ly + m[5]
                px, py = int(round(wx - 0.5)), int(round(wy - 0.5))
                got = tuple(srows[py][px * 4 + k] for k in range(4))
                want = expected_pixel(sdoc, px, py)
                if max(abs(a - b) for a, b in zip(got, want)) > 2:
                    raise AssertionError(f"stretched pixel mismatch at local ({lx},{ly}): go={got} python={want}")
                # world point inverse-maps back to the same local ray parameter
                t_local = radial_t(sbrush, lx, ly)
                det = m[0] * m[3] - m[1] * m[2]
                qx = (m[3] * (wx - m[4]) - m[2] * (wy - m[5])) / det
                qy = (-m[1] * (wx - m[4]) + m[0] * (wy - m[5])) / det
                assert abs(radial_t(sbrush, qx, qy) - t_local) < 1e-12

            # 5. Designer edits the highlight: new revision, new image, old
            #    artifact remains immutable.
            revised = copy.deepcopy(doc)
            revised["layers"][0]["shapes"][0]["brush"]["focus"] = {"x": -12, "y": 10}
            api.call("PUT", "/v1/plates/check-plate",
                     {"expected_revision": 1, "document": revised}, status=200)
            art2 = api.render("check-plate", "png", revision=2)
            assert art2["digest"] != art["digest"]
            again = api.render("check-plate", "png", revision=1)
            assert again["digest"] == art["digest"], "old revision image changed"

            # 6. Degenerate geometry is refused at admission.
            bad_radius = controlled_document("bad-radius", [1, 0, 0, 1, 0, 0], focus)
            bad_radius["document"]["layers"][0]["shapes"][0]["brush"]["radius_y"] = 0
            api.call("POST", "/v1/plates", bad_radius, status=400)
            bad_focus = controlled_document("bad-focus", [1, 0, 0, 1, 0, 0], {"x": 30, "y": 0})
            api.call("POST", "/v1/plates", bad_focus, status=400)
            outside_focus = controlled_document("outside-focus", [1, 0, 0, 1, 0, 0], {"x": 0, "y": 31})
            api.call("POST", "/v1/plates", outside_focus, status=400)

            # 7. SVG carries the same normalized focal point and ellipse.
            svg_art = api.render("check-plate", "svg", revision=1)
            svg = api.raw("/v1/artifacts/" + svg_art["id"]).decode()
            assert "<radialGradient" in svg and "gradientTransform=\"matrix(30 0 0 30 40 40)\"" in svg
            assert 'fx="0.333333333"' in svg and 'fy="-0.266666667"' in svg
            assert "<script" not in svg and "foreignObject" not in svg

            # 8. Shipped example: deterministic digest, no exterior dark ring.
            example = json.loads((ROOT / "examples/radial-spot.json").read_text())
            api.call("POST", "/v1/plates", example, status=201)
            e1 = api.render("radial-spot", "png")
            e2 = api.render("radial-spot", "png")
            assert e1["digest"] == e2["digest"]
            ew, eh, erows = read_png(api.raw("/v1/artifacts/" + e1["id"]))
            assert (ew, eh) == (160, 120)
            ebg = example["document"]["background"]
            exp_bg = tuple(int(math.floor(linear_to_srgb(srgb_to_linear(c)) * 255 + 0.5))
                           for c in (ebg["r"], ebg["g"], ebg["b"])) + (255,)
            for px, py in [(1, 1), (159, 1), (1, 119), (159, 119)]:
                got = tuple(erows[py][px * 4 + k] for k in range(4))
                assert got == exp_bg, ("example dark ring", px, py, got, exp_bg)
            esvg = api.raw("/v1/artifacts/" + api.render("radial-spot", "svg")["id"]).decode()
            assert esvg.count("<radialGradient") == 2 and "gradientTransform" in esvg

            # 9. Bundle export and import into a fresh persistence root.
            envelope = api.raw("/v1/plates/radial-spot/bundle")
            stop()
            fresh_root = base / "state2"
            start(fresh_root)
            req = urllib.request.Request(url + "/v1/bundles", data=envelope, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=35) as r:
                assert r.status == 201
            imported = api.render("radial-spot", "png")
            assert imported["digest"] == e1["digest"], "bundle import changed rendering"

            # 10. Restart on the original root: recovered plate and identical
            #     artifact bytes, proving the new fields survive persistence.
            stop()
            start(data_root)
            recovered = api.call("GET", "/v1/plates/check-plate")
            assert len(recovered["revisions"]) == 2
            assert recovered["revisions"][1]["document"]["layers"][0]["shapes"][0]["brush"]["focus"] == {"x": -12, "y": 10}
            assert api.render("check-plate", "png", revision=1)["digest"] == art["digest"]
            assert api.render("check-plate", "png", revision=2)["digest"] == art2["digest"]

            print(json.dumps({"workflow": "radial", "result": "passed",
                              "pixel_probes": len(probes), "port": port}, ensure_ascii=False))
        finally:
            stop()
            log.close()


if __name__ == "__main__":
    main()
