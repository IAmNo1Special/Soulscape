#version 330

in vec3 v_position;
in vec3 v_normal;
out vec4 fragColor;
uniform float time;
uniform float base_brightness;
uniform vec3 base_color_uniform; // New uniform for custom base color


void main() {
    // Calculate distance from center and height factor
    float dist = length(v_position);
    // Map y position to 0-1 range, with 0 at the bottom and 1 at the top
    float heightFactor = (v_position.y + 1.0) * 0.5; // Map from [-1,1] to [0,1]
    
    // Create vertical gradient with more intensity at the bottom
    float intensity = 0.6;
    float baseAlpha = smoothstep(1.0, 0.2, dist) * intensity;
    
    // Create a more flame-like alpha distribution
    float alpha = baseAlpha * (0.7 + 0.3 * sin(dist * 10.0 - time * 3.0));
    // Remove the bottom fade out to ensure full coverage
    alpha *= smoothstep(0.0, 0.1, 1.0);
    
    // Flame color gradient from base_color_uniform at base to a tinted version at tip
    // Now using base_color_uniform for the base color and mixing it with a lighter version
    vec3 base_color = mix(
        base_color_uniform, // Use the provided uniform color at the base
        base_color_uniform + vec3(0.2, 0.2, 0.2), // A slightly lighter/whiter version at the top
        pow(heightFactor, 0.7)  // Non-linear gradient
    );
    
    // Add subtle pulsing highlights
    float pulse1 = 0.7 + 0.3 * sin(time * 1.0 + v_position.y * 3.0);  
    float pulse2 = 0.7 + 0.3 * sin(time * 1.2 + v_position.y * 2.5);  
    
    // Add more dynamic color variation, derived from the base_color_uniform
    vec3 highlight1 = base_color_uniform + vec3(0.3, 0.3, 0.3); // Lighter version of base
    vec3 highlight2 = base_color_uniform * 0.8 + vec3(0.2, 0.2, 0.2); // Slightly desaturated and brighter
    
    // Blend colors based on position
    vec3 final_color = mix(
        mix(base_color, highlight1, pulse1 * 0.3),
        highlight2,
        pulse2 * 0.2 * heightFactor
    );
    
    // Add rim lighting for depth, also tinted by the base color
    float rim = 1.0 - abs(dot(normalize(v_normal), normalize(-v_position)));
    rim = smoothstep(0.3, 1.0, rim);
    final_color += rim * base_color_uniform * 0.5; // Use base color for rim light tint
    
    // Ensure brightness is in a good range
    final_color = clamp(final_color * base_brightness, vec3(0.0), vec3(2.0)); // Clamped min to 0.0 for darker colors

    // Final alpha with edge falloff
    alpha *= smoothstep(0.0, 0.2, 1.0 - dist); // Fade out at edges
    alpha *= 1.5; // Increase overall visibility
    
    fragColor = vec4(final_color, alpha);
}
