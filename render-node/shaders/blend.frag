// blend.frag — per-edge soft-edge blend + black-level lift + per-node color trim,
// plus procedural test patterns that travel through the same warp.
//
// Pipeline order, applied per fragment:
//   1. fetch source colour (video texture) OR generate a test pattern
//   2. colour trim   : out = pow(c, 1/gamma) * gain + lift
//   3. edge blend    : multiply by the soft-edge alpha for left/right/top/bottom
//   4. black lift    : raise the floor outside the overlap so a lone projector's
//                      black matches the doubled black of an overlap
//
// v_edge is 0..1 across THIS node's own output, so all edge math is in the
// node's own frame and is independent of the warp.

precision highp float;

varying vec2 v_uv;     // source coordinate (inside source_region)
varying vec2 v_edge;   // 0..1 across this node's output

uniform sampler2D u_tex;
uniform sampler2D u_chroma;
uniform int u_nv12;
uniform int u_wrap;
uniform mat3 u_yuv_matrix;
uniform vec3 u_yuv_offset;

// Edge blend. Each edge: x = width (fraction of output, 0 disables), y = gamma.
uniform vec2 u_blend_left;
uniform vec2 u_blend_right;
uniform vec2 u_blend_top;
uniform vec2 u_blend_bottom;
// Black-level lift per edge (added inside that edge's non-overlap region).
uniform vec4 u_black_lift;   // left, right, top, bottom

// Colour trim.
uniform vec3 u_gain;
uniform float u_gamma;
uniform vec3 u_lift;

// Test pattern: 0=video, 1=grid, 2=crosshair, 3=grey, 4=white, 5=color, 6=source-grid.
uniform int u_pattern;
uniform vec4 u_tile_region;     // local tile UV -> global panorama coordinates
uniform vec3 u_pattern_color;   // for kind=color
uniform vec2 u_tex_size;        // source size in px, for crisp grid lines

// One edge's blend alpha: ramps 0->1 across the overlap band of given width.
// coord is the distance from this edge in 0..1 (0 at the edge, 1 at far side).
float edge_alpha(float coord, float width, float gamma) {
    if (width <= 0.0) return 1.0;
    float t = clamp(coord / width, 0.0, 1.0);
    // Projector light output is approximately encoded_value^gamma. Convert
    // the linear light weight back to an encoded multiplier, so complementary
    // weights add to constant light instead of producing a dark overlap band.
    return pow(t, 1.0 / gamma);
}

vec3 test_pattern() {
    if (u_pattern == 6) {
        // Recover global coordinates even when the current video is a cropped
        // tile. Identical panorama positions must produce identical marks on
        // neighbouring projectors, including positions beyond the 360° seam.
        vec2 source = u_tile_region.xy + v_uv * u_tile_region.zw;
        vec2 cell = vec2(fract(source.x) * 36.0, source.y * 8.0);
        vec2 distance = min(fract(cell), 1.0 - fract(cell));
        float grid = 1.0 - step(0.006, distance.x) * step(0.0125, distance.y);
        // Distinct ten-degree bands disambiguate repeated grid intersections.
        vec3 key = fract(floor(cell.x) * vec3(0.618034, 0.414214, 0.732051)
                         + vec3(0.2, 0.5, 0.8));
        vec3 background = vec3(0.08) + 0.4 * key + 0.03 * mod(floor(cell.y), 2.0);
        return mix(background, vec3(1.0), grid);
    }
    if (u_pattern == 3) return vec3(0.5);
    if (u_pattern == 4) return vec3(1.0);
    if (u_pattern == 5) return u_pattern_color;
    if (u_pattern == 2) {
        // crosshair: thin cross through the source centre + a centre dot
        vec2 d = abs(v_uv - 0.5);
        float line = min(d.x, d.y);
        float c = step(line, 0.0015);
        return vec3(c);
    }
    // grid (kind 1): numbered grid is drawn host-side as a real texture; here we
    // render a clean line grid as a fallback so calibration always has geometry.
    vec2 cell = v_uv * 16.0;
    vec2 g = abs(fract(cell) - 0.5);
    float line = min(g.x, g.y);
    float lw = 0.5 - (1.0 / 32.0);           // ~1 cell-fraction line width
    float on = step(lw, line) ;
    // bright border so the node's source extent is visible
    vec2 b = step(v_uv, vec2(0.004)) + step(vec2(0.996), v_uv);
    float border = clamp(b.x + b.y, 0.0, 1.0);
    return vec3(max(on, border));
}

void main() {
    vec3 c;
    if (u_pattern == 0) {
        vec2 uv = vec2(u_wrap == 1 ? fract(v_uv.x) : v_uv.x, v_uv.y);
        if (u_nv12 == 1) {
            vec2 chroma = texture2D(u_chroma, uv).ra;
            c = u_yuv_matrix * (vec3(texture2D(u_tex, uv).r, chroma) + u_yuv_offset);
        } else {
            c = texture2D(u_tex, uv).rgb;
        }
    } else {
        c = test_pattern();
    }

    // 2. colour trim
    c = pow(max(c, 0.0), vec3(1.0 / u_gamma)) * u_gain + u_lift;

    // 3. edge blend — multiply alphas so corners fall off in both axes
    float aL = edge_alpha(v_edge.x,        u_blend_left.x,   u_blend_left.y);
    float aR = edge_alpha(1.0 - v_edge.x,  u_blend_right.x,  u_blend_right.y);
    float aT = edge_alpha(1.0 - v_edge.y,  u_blend_top.x,    u_blend_top.y);
    float aB = edge_alpha(v_edge.y,        u_blend_bottom.x, u_blend_bottom.y);
    float alpha = aL * aR * aT * aB;
    c *= alpha;

    // 4. black-level lift: where this projector is the sole contributor (outside
    // the overlap, alpha==1) raise the floor by the per-edge lift so its black
    // matches the doubled black seen inside the overlap.
    float liftAmt = 0.0;
    liftAmt = max(liftAmt, u_black_lift.x * step(u_blend_left.x,   v_edge.x));
    liftAmt = max(liftAmt, u_black_lift.y * step(u_blend_right.x,  1.0 - v_edge.x));
    liftAmt = max(liftAmt, u_black_lift.z * step(u_blend_top.x,    1.0 - v_edge.y));
    liftAmt = max(liftAmt, u_black_lift.w * step(u_blend_bottom.x, v_edge.y));
    c = max(c, vec3(liftAmt) * alpha + vec3(liftAmt) * (1.0 - alpha));

    gl_FragColor = vec4(clamp(c, 0.0, 1.0), 1.0);
}
