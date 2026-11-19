// Package paint implements linear-light, premultiplied-alpha compositing.
package paint

import (
	"math"
	"prismplate/internal/geom"
	"prismplate/internal/scene"
)

type Pixel struct{ R, G, B, A float64 }

func linear(v float64) float64 {
	if v <= 0.04045 {
		return v / 12.92
	}
	return math.Pow((v+0.055)/1.055, 2.4)
}
func srgb(v float64) float64 {
	v = math.Max(0, math.Min(1, v))
	if v <= 0.0031308 {
		return v * 12.92
	}
	return 1.055*math.Pow(v, 1/2.4) - 0.055
}
func From(c scene.Color) Pixel {
	return Pixel{linear(c.R) * c.A, linear(c.G) * c.A, linear(c.B) * c.A, c.A}
}
func (p Pixel) Scale(a float64) Pixel { return Pixel{p.R * a, p.G * a, p.B * a, p.A * a} }
func (p Pixel) RGBA8() (uint8, uint8, uint8, uint8) {
	if p.A <= 0 {
		return 0, 0, 0, 0
	}
	return uint8(math.Round(srgb(p.R/p.A) * 255)), uint8(math.Round(srgb(p.G/p.A) * 255)), uint8(math.Round(srgb(p.B/p.A) * 255)), uint8(math.Round(math.Min(1, p.A) * 255))
}
func mix(a, b, t float64) float64 { return a + (b-a)*t }

// radialT maps a local point to the 0..1 stop parameter of an elliptical
// radial gradient with an off-center focal point, matching the SVG radial
// gradient ray model so PNG and vector proofs share one geometry.
//
// The ellipse center C, radii (rx, ry) and focal offset F are authored in
// shape-local coordinates. After normalizing by the radii the ending ellipse
// becomes the unit circle and the focal point is f = F/(rx, ry), guaranteed by
// validation to satisfy |f|^2 < 1. Points are parameterized as f + s*d with
// d = u-f, so the sample point u sits at s=1. Solving |f + s*d|^2 = 1 gives
// the positive ray-to-circle factor S; the stop parameter is t = 1/S. Hence
// the focus maps to t=0, the ending ellipse to t=1, interior points stay in
// (0,1) and a centered focus (f=0) degenerates to the plain distance from the
// center. Because the focus is strictly inside, the quadratic has one positive
// root and its discriminant is non-negative. Points outside the ellipse yield
// t>1 and are clamped to the last stop exactly like linear gradients.
func radialT(b scene.Brush, p geom.Point) float64 {
	ux := (p.X - b.Center.X) / b.RadiusX
	uy := (p.Y - b.Center.Y) / b.RadiusY
	fx := b.Focus.X / b.RadiusX
	fy := b.Focus.Y / b.RadiusY
	dx, dy := ux-fx, uy-fy
	d2 := dx*dx + dy*dy
	if d2 == 0 {
		return 0
	}
	bb := 2 * (fx*dx + fy*dy)
	c := fx*fx + fy*fy - 1
	disc := bb*bb - 4*d2*c
	if disc < 0 {
		disc = 0
	}
	s := (-bb + math.Sqrt(disc)) / (2 * d2)
	if s <= 0 {
		return 0
	}
	return math.Min(1, 1/s)
}
func Sample(b scene.Brush, p geom.Point) Pixel {
	if b.Kind == "solid" {
		return From(b.Color)
	}
	var t float64
	if b.Kind == "radial" {
		t = radialT(b, p)
	} else {
		dx, dy := b.To.X-b.From.X, b.To.Y-b.From.Y
		t = ((p.X-b.From.X)*dx + (p.Y-b.From.Y)*dy) / (dx*dx + dy*dy)
	}
	t = math.Max(0, math.Min(1, t))
	for i := 1; i < len(b.Stops); i++ {
		a, z := b.Stops[i-1], b.Stops[i]
		if t <= z.At {
			u := (t - a.At) / (z.At - a.At)
			x, y := From(a.Color), From(z.Color)
			return Pixel{mix(x.R, y.R, u), mix(x.G, y.G, u), mix(x.B, y.B, u), mix(x.A, y.A, u)}
		}
	}
	return From(b.Stops[len(b.Stops)-1].Color)
}
func Over(dst, src Pixel, mode string) Pixel {
	blend := func(d, s float64) float64 {
		if mode == "normal" || src.A == 0 || dst.A == 0 {
			return s + d*(1-src.A)
		}
		a, b := d/dst.A, s/src.A
		v := a * b
		if mode == "screen" {
			v = a + b - a*b
		}
		return (1-src.A)*d + (1-dst.A)*s + src.A*dst.A*v
	}
	return Pixel{blend(dst.R, src.R), blend(dst.G, src.G), blend(dst.B, src.B), src.A + dst.A*(1-src.A)}
}
