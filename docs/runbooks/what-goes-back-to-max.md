# Runbook: what goes back to Max

Use when a client or a creative director asks for a change and you are not sure whether PLATE's AI may make it. The short answer: **anything that lives in a locked pixel is Max work**, and the AI finishes only what is left around it.

**Why.** The packet's policy marks each ID editable or locked, by layer name. Editable: sky (`SKY`), vegetation (`VEG_`, `GRASS`), water (`WATER`), people (`PEOPLE`) and furniture (`ENT_`). Locked: architecture (`ARCH_`), glazing (`GLZ_`, `GLAZING`), signage (`SIGN_`), ground (`HARD_`, `GRND_`), vehicles (`VEH_`), a node on layer `0`, a node on a layer that matches nothing, and anything tagged `architecture`, `glazing` or `signage`. After every Edit the composite puts the plate back everywhere outside the editable mask, bit for bit. So a generator that draws a garland on the facade, a puddle on the paving or a car in the drive is paid for and then discarded: the drawing never reaches the delivered picture. There is no override for a locked ID, and no text-to-image path to get around it.

## 1. The list

| the change | why the AI cannot make it | what Max does | the AI finishing that follows |
|---|---|---|---|
| **Decorations on architecture** (garlands, banners, festival lights on a facade, wall graphics) | facade pixels are architecture, locked | model the decoration and put it on the layer that says who may touch it: `ARCH_` if it must stay bit-exact, `ENT_` if the AI may finish its light. Add its own lights if it glows | **Festive dress** for the planting, furniture, people and sky around it (only where the render already has them); the night chain if it is a night shot |
| **New fixtures** (wall lights, uplights, pendants, bollards) | the light on a locked surface is the render's, and a fixture is geometry plus a light | model the fixture, add its V-Ray light, and give it **its own light select**: LightMix can only mix lights that exist as separate passes | the **Tier A night chain** (LightMix, then the night relight edit) repaints sky, planting and people to match the light V-Ray made |
| **Wet facades** (rain-wet stone, rendered walls, glass with droplets) | walls and glazing are locked | a wet material variant: darker diffuse, a glossy coat, streaks from a blend or dirt map, glass with its droplet bump | **Monsoon pass**: rain, mist, the sky, saturated planting |
| **Texture and tile swaps** (a different paving pattern, tile size, brick bond, timber species) | the pattern is the material of a locked surface | a new material in Max, re-render. The **Recolour** node can change a material's *colour* to a RAL or hex target by recomposing the render's own elements (with an expected ΔE shown first); it cannot change the pattern or the scale. A Reference of role `swatch` is a colour or mood sample for an engine inside its mask, not a retexture | nothing on the surface itself; the sky, planting and people around it |
| **Cars** | `VEH_` is locked, and a generator gets wheels, reflections and number plates wrong | a modelled car (a proxy is fine) on a `VEH_` layer, with real V-Ray reflections | **Plaza staffage** or another people recipe for the people around it. Never the car |
| **Foreground branches** (overhanging branches that frame the shot) | a new branch has to cover facade or glazing pixels that are locked, and depth of field needs real geometry | a foreground tree or branch proxy on a `VEG_` layer, rendered with the camera's depth of field | **Planting refresh** (`[PLANT SPECIES]` named): now the branch is a vegetation ID, so the AI may upgrade its foliage |
| **Hardscape** (paving, steps, kerbs, paths, road markings, ground decals, leaf litter, snow on the ground) | `HARD_` and `GRND_` are ground, locked | change the geometry or material in Max; for leaf litter or snow, scatter or blend it onto the ground | sky, planting, people and water around it: Season swap, Monsoon pass, a relight |

The same rule covers anything else on a locked layer: signage text, glass tint or reflectance, a changed facade, an interior wall the policy never marks editable. If you can name the layer it lives on and that layer starts `ARCH_`, `GLZ_`, `SIGN_`, `HARD_`, `GRND_` or `VEH_`, the change is a Max change.

## 2. How to send the change back

1. **Make the change in Max, on the right layer.** The layer prefix decides the class, and the class decides what the AI may do afterwards. Put a decoration the AI must leave alone on an `ARCH_` layer; put one it may finish (a plant, a cushion, a lantern) on its own class's layer. Never leave a new object on layer `0`: it is unlabelled, locked, and counts against the `UNLABELLED_SHARE` lint.
2. **Send with `supersedes last` ticked** (same camera, same project). The new packet supersedes the old one, and the approved AI layers whose IDs did not change carry over by themselves (auto carry-over is on by default): they are re-composited on the new plate and re-gated, and the daemon publishes `packet.carried` with the counts. A carried layer is a new run you still have to Accept after a scrub. A layer that touched an ID you changed is dropped with the reason (`material changed`). If auto carry-over is off, select the new Plate and press **Carry over approved layers from <old packet>**.
3. **Re-run only what was dropped.** Open each dropped node, Quote and Make. **Send + quote** does the first step for you: it sends, lays an `edit_chain` on the canvas for the new packet and quotes the Edit (artist manual section 17).
4. **Light changes need light passes.** If the change is a light (a fixture, a decoration that glows), send a packet that has the light selects, or LightMix has nothing to mix (`no_light_selects`).
5. **Check the lint before you render.** `UNLABELLED_SHARE` stops a Send when too much of the frame sits on no labelled layer; a new object on layer `0` is the usual cause.

## 3. What not to do

- Do not try to get the AI to do it with a stronger prompt, a reference image or a wider mask. A reference is an engine input, never pixels of the output, and the mask cannot include a locked ID.
- Do not remove the `architecture`, `glazing` or `signage` tag from a node, or rename its layer, to make it editable. That moves the doctrine, not the picture: a facade you unlock is a facade the AI may redraw, which is exactly what the policy exists to prevent.
- Do not paint the change in retouch and Import it as an AI take. If it was made by hand, import it as `manual` ([manual-pipeline-fallback.md](manual-pipeline-fallback.md)): it is gated and ledgered as what it is.
- Do not ask for a camera move to hide the change. A new angle is a Max render ([two-point-perspective.md](two-point-perspective.md) for tilted cameras), never an AI invention.

## 4. Afterwards

Put the change and the layer it landed on in the project notes (the next revision will touch the same objects), and, if this was the third time a client asked for the same thing, add it to the scene's standard layers so it is labelled from the start.
