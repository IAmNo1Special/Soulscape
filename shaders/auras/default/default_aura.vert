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



// Re-use noise functions

float noise(vec2 coord) {

  return fract(sin(dot(coord, vec2(12.9898, 78.233))) * 43758.5453);

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

  v_normal = normal; // Pass normal to fragment shader



  // Base noise displacement (similar to orb, but scaled for aura)

  float noiseDisplacement = pnoise(position.xy * 2.5 + time * 0.7) * 0.08;

  noiseDisplacement += pnoise(position.yz * 3.5 + time * 0.9) * 0.05;

  noiseDisplacement += pnoise(position.xz * 4.5 + time * 1.1) * 0.03;

 

  // Bulge displacement (similar to orb)

  float distToBulge = distance(position, bulge_position);

  float bulgeFalloff = smoothstep(0.5, 0.0, distToBulge);

  float bulgeDisplacement = bulgeFalloff * bulge_strength;



  float totalDisplacement = noiseDisplacement + bulgeDisplacement;



  // Apply displacement along the normal, scaled for aura

  vec3 displacedPosition = position + normal * totalDisplacement * 1.2;



  // Flame-like shape at the top

  float yInfluence = smoothstep(0.0, 1.0, (displacedPosition.y + 0.3) / 0.6);



  // Add a swirling, upward displacement for the flame effect

  float flameDisplacement = pnoise(displacedPosition.xz * 5.0 + time * 2.0) * 0.15;

  flameDisplacement += pnoise(displacedPosition.xy * 7.0 + time * 1.5) * 0.1;

 

  // Scale flame displacement by y-influence, making it stronger at the top

  flameDisplacement *= yInfluence * 2.0;



  // Calculate height factor for the entire sphere

  float heightFactor = (position.y + 1.0) * 0.5; // Map from [-1,1] to [0,1]

 

  // Apply flame displacement with more movement at the top and reduce height

  float verticalDisplacement = flameDisplacement * (0.3 + 0.3 * sin(time * 2.0)); // Reduced vertical movement

  float heightScale = 0.7; // Reduce overall height by 30%

  displacedPosition.y = (displacedPosition.y * heightScale) + (verticalDisplacement * heightFactor * heightScale);

 

  // Create a smooth taper from bottom to top

  float taperFactor = pow(heightFactor, 1.5); // Increased exponent for faster tapering

 

  // Add dynamic movement to the sides

  float noise = sin(time * 3.0 + position.y * 8.0) * cos(time * 2.5 + position.y * 6.0);

 

  // Apply side movement that decreases towards the bottom

  // Reduce overall side movement and make it more concentrated at the top

  float sideMovement = 0.2 * taperFactor * (0.5 + 0.5 * heightFactor);

  displacedPosition.x += noise * sideMovement;

  displacedPosition.z += cos(time * 2.8 + position.y * 7.0) * sideMovement;

 

  // Create a very tight but rounded bottom for the aura

  float bottomTightness = 0.98; // Very tight fit



  // Create a sharp but rounded curve at the very bottom

  float bottomCurve = smoothstep(0.0, 0.3, 1.0 - heightFactor);

  // Create a dome shape that's very flat at the bottom

  float domeShape = 1.0 - pow(1.0 - heightFactor, 0.5);



  // Combine with extra tightness at the very bottom

  float tightness = mix(0.8, bottomTightness, smoothstep(0.0, 0.2, 1.0 - heightFactor));

  float baseScale = 0.7; //

  float scale = baseScale * (1.0 - tightness * bottomCurve * domeShape);



  // Apply scaling to the base

  displacedPosition.xz *= scale + 0.01; // Reduced from 0.02 for tighter base

 

  // Create a more pronounced pinch at the top

  float pinchFactor = smoothstep(0.3, 0.9, heightFactor); // Start pinching earlier

  float pinchStrength = 0.9; // Increased from 0.7 for more pronounced pinch

 

  // Apply pinch effect - more aggressive at the top

  float pinchAmount = pinchFactor * pinchStrength;

  float pinchCurve = 1.0 - pinchAmount * (1.0 - smoothstep(0.8, 1.0, heightFactor));

  displacedPosition.xz *= pinchCurve;

 

  // Add extra upward pull at the very top for a flame-like tip

  float tipPull = smoothstep(0.9, 1.0, heightFactor) * 0.3;

  displacedPosition.y += tipPull * pinchFactor;



  v_position = displacedPosition;

  gl_Position = projection * view * model * vec4(displacedPosition, 1.0);

}