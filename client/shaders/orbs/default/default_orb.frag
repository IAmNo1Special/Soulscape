#version 330



in vec3 v_position;

in vec3 v_normal;



out vec4 fragColor;



uniform float time;

uniform vec3 base_color_uniform; // New uniform for custom base color

uniform float desat_factor; // Issue #29: 0 full color .. 1 full grayscale

uniform float brightness; // Issue #29: biology/reflex brightness multiplier

uniform float opacity; // Issue #29: typing-dip / reflex alpha multiplier

uniform float pulse_rate; // Issue #29: plasma-pulse speed multiplier

uniform float pulse_strength; // Issue #29: plasma-pulse amplitude multiplier



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

  vec2 uv = v_position.xy;

  float dist = length(uv);

 

  // Plasma effect (issue #29: pulse_rate scales the time terms,
  // pulse_strength scales the highlight mix; 0 freezes the pattern)
  float pt = time * pulse_rate;

  float plasma = pnoise(uv * 10.0 + pt * 0.5) * 0.5;

  plasma += pnoise(uv * 20.0 - pt * 0.3) * 0.3;

  plasma = fract(plasma);

  plasma = plasma * pulse_strength + 0.5 * (1.0 - pulse_strength);

 

  // Base color (now using the uniform)

  vec3 base_color = base_color_uniform;

  vec3 highlight = vec3(0.56, 0.93, 0.56); // Light green

 

  // Mix base color with highlight based on plasma

  vec3 final_color = mix(base_color, highlight, smoothstep(0.6, 0.7, plasma));

 

  // Add glow (reduced intensity to prevent saturation)
  float glow = 1.0 - smoothstep(0.0, 0.5, dist);
  final_color += vec3(0.3, 0.4, 0.6) * glow * 0.3;
  
  // Boost brightness but preserve color ratios
  float luminance = dot(final_color, vec3(0.299, 0.587, 0.114));
  float boost = mix(1.4, 1.0, luminance);  // Less boost for brighter colors
  final_color *= boost;

  // Issue #29: state-driven desaturation, then brightness
  vec3 gray = vec3(dot(final_color, vec3(0.299, 0.587, 0.114)));
  final_color = mix(final_color, gray, desat_factor);
  final_color *= brightness;

  fragColor = vec4(clamp(final_color, 0.0, 1.0), opacity);

}