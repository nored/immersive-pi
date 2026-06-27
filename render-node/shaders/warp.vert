// warp.vert — NxM control-point mesh warp.
//
// The CPU builds a triangulated grid from the room-model mesh. Each vertex
// already carries its destination clip-space position (a_pos), the source
// texture coordinate to sample (a_uv, pre-mapped into the node's
// source_region), and a normalized output coordinate (a_edge, 0..1 across the
// node's own projector) used by the fragment stage for edge blending.
//
// The warp is therefore data: move the control points in the editor and the
// rebuilt vertex buffer bends the image. No projection math lives in the shader.

attribute vec2 a_pos;   // output clip-space position, [-1,1]
attribute vec2 a_uv;    // source texture coordinate, already inside source_region
attribute vec2 a_edge;  // normalized position across this node's output, 0..1

varying vec2 v_uv;
varying vec2 v_edge;

void main() {
    v_uv = a_uv;
    v_edge = a_edge;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
