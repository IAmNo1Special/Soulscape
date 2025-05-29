#version 330



in vec3 position;

in vec3 normal;



out vec3 v_position;

out vec3 v_normal;



uniform mat4 model;

uniform mat4 view;

uniform mat4 projection;

uniform float time;

uniform vec3 bulge_position;

uniform float bulge_strength;



float noise(vec2 p) {

  return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);

}



float pnoise(vec2 p) {

  vec2 ip = floor(p);

  vec2 fp = fract(p);

  fp = fp * fp * (3.0 - 2.0 * fp);

 

  float n00 = noise(ip + vec2(0.0, 0.0));

  float n10 = noise(ip + vec2(1.0, 0.0));

  float n01 = noise(ip + vec2(0.0, 1.0));

  float n11 = noise(ip + vec2(1.0, 1.0));

 

  float x0 = mix(n00, n10, fp.x);

  float x1 = mix(n01, n11, fp.x);

  return mix(x0, x1, fp.y);

}



void main() {

  // Reintroducing subtle noise displacement

  float noise_disp = pnoise(position.xy * 2.5 + time * 0.7) * 0.005; // Reduced from 0.08

  noise_disp += pnoise(position.yz * 3.5 + time * 0.9) * 0.003; // Reduced from 0.05

  noise_disp += pnoise(position.xz * 4.5 + time * 1.1) * 0.002; // Reduced from 0.03

 

  // Reintroducing subtle bulge displacement

  float dist_to_bulge = distance(position, bulge_position);

  float bulge_falloff = smoothstep(0.5, 0.0, dist_to_bulge);

  float bulge_disp = bulge_falloff * bulge_strength * 0.1; // Reduced bulge influence significantly



  // Combine displacements

  float total_disp = noise_disp + bulge_disp;

 

  // Apply displacement

  vec3 new_position = position + normal * total_disp;

 

  // Outputs

  v_position = new_position;

  v_normal = normalize(normal);

  gl_Position = projection * view * model * vec4(new_position, 1.0);

}