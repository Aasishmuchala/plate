# Runbook: weather, seasons and occasions

Use when a client asks for rain, mist, a season, a festival dressing or "make it look like winter", and you need to know which recipe to run, what it can and cannot change, and when the answer is a different V-Ray render.

**The rule that shapes everything:** AI changes only the IDs the packet marks editable, and the composite throws every other pixel back to the render. The editable classes are sky, vegetation, water, people and furniture (layers `SKY`, `VEG_`, `GRASS`, `WATER`, `PEOPLE`, `ENT_`). Architecture, glazing, signage, ground (`HARD_`, `GRND_`), vehicles and anything unlabelled are locked. So the weather AI can make is the weather that lives in the sky, the planting, the water and the people. Weather that lives in the ground, the facade, the glass or the roofs is a V-Ray job. And **the light on the building is the render's**: a recipe can put a grey sky over the scene, but it cannot soften the shadows that are already on the facade.

## 1. Which recipe

All of these are Edit recipes on Nano Banana 2.1 (shipped, group in brackets); pick one in the Edit editor, fill its tokens, read the Assembled prompt, then Quote ([artist manual](../artist-manual.md) section 16).

| the ask | recipe (library id) | edits | token (default) |
|---|---|---|---|
| a different hour inside one light family (morning to afternoon) | Same-family relight (a01) [lighting] | sky, vegetation, water, people | `[CURRENT HOUR]` (mid-morning) and `[TARGET HOUR]` (late afternoon). One or two attempts |
| day to dusk or night | Twilight hero (a03), Night exterior (a10) [lighting], or the Tier A night chain | sky, vegetation, water, people | Twilight hero: `[2700K]` (the colour temperature of the lit interiors), `[WATER FEATURE]`. Cross-family changes are honest about their odds: budget three to five attempts (Cross-family relight has `[FROM LIGHT]`, `[TO LIGHT]` and `[SKY STATE]`). The night chain (artist manual section 17) lets V-Ray's light selects light the building and uses the generator only for the world around it |
| heavy rain, mist, a low grey sky | Monsoon pass (a04) [weather] | sky, vegetation, water, people | none. Rain in fine sheets, saturated planting, mist in the far background, interior lights reading against the gloom |
| spring, summer, autumn or winter | Season swap (a05) [season] | vegetation, sky, water | `[SEASON]` (autumn). The same trees in another month, not other trees |
| Diwali or another occasion | Festive dress [season] | vegetation, people, sky, furniture | `[OCCASION]` (Diwali). Decoration goes only where the render already has planting, furniture or people |
| better planting (not weather, but it often follows) | Planting refresh (a09) [vegetation] | vegetation | `[PLANT SPECIES]`: name the species ("rain tree: feathery bipinnate leaflets, vast umbrella canopy") or you get the species already planted drawn true to kind; `[LEAF CONDITION]` carries the weather onto the leaves ("wet, glossy, rain-darkened undersides" for a monsoon frame; the default is dry and dusty); `[IMPERFECTIONS]` |

For haze or depth mist, use the **Atmosphere** node instead of a recipe (artist manual section 13): it computes fog from the packet's own Z, so nearer things are clearer than far ones by physics rather than by a model's guess. For a warmer or cooler overall look, use **Colour**. For a replaced sky from a photograph, use **Sky** with a Reference of role `sky`. These are numeric nodes: no generator, no price.

## 2. What needs a different V-Ray packet

| effect | AI can do it? | what it takes |
|---|---|---|
| rain falling in the air, mist behind the building, a low grey sky, saturated and bowing planting | yes (Monsoon pass) | nothing more |
| **wet ground**: dark paving, puddles, mirror reflections of the sky and the building | **no**: the ground is locked | a V-Ray packet in which the ground materials are wet (a darker diffuse, a glossy reflective coat, puddle blend or bump) with the reflections rendered. The recipe asks for mirror reflections "where a surface is wet", and that only lands on a surface the packet marks editable |
| wet facade, rain streaks, droplets on glazing | **no**: architecture and glazing are locked | a wet material variant in Max |
| **snow** on ground, roofs, ledges, benches, paving | **no**: all locked | a V-Ray packet with the snow modelled or blended onto the top faces. Then Season swap with `[SEASON]` winter finishes the trees (bare branches, winter sky) and the sky |
| autumn leaf litter on paving, petals, puddled leaves | **no**: ground is locked | scattered leaf geometry in Max on a ground layer. Season swap does the canopy |
| overcast or low-contrast light on the building | **no**: the light on locked pixels is the render's | an overcast V-Ray plate (sun off, sky light only). Then Monsoon pass adds the rain, the mist and the planting |
| a dusk or night building | not for the building | the Tier A night chain, or a V-Ray night render |

If the answer is "a different V-Ray packet", the steps are in [what-goes-back-to-max.md](what-goes-back-to-max.md). Do not try to talk the generator into it: the engine may draw wet paving or snow, and the composite will discard it, because those pixels are locked. You will have paid for a take whose best part was thrown away.

## 3. The procedure

1. **Check the legend.** Select the Plate and open the Mask editor: the sky, planting, water and people the recipe needs must exist as labelled IDs. A scene with no `WATER` layer gives the water class nothing to do; unlabelled trees are locked.
2. **Settle the plate first.** If the effect needs wet ground, snow or overcast light, get that packet from Max (tick `supersedes last` so approved layers carry over) before you spend anything here.
3. **Lay the chain.** Apply `edit_chain` to the packet (Projects tab, **Apply template…**) or wire Plate, Mask, Edit by hand. Pick the recipe, fill its token, read the Assembled prompt, and check the Make bar's chips and engine card.
4. **Make two to four takes.** Weather is a lottery; N 2 to 4 under one quote costs the same as four separate tries and ranks them for you. Scrub at 100% and Accept the best.
5. **A season set.** One frame, four runs with `[SEASON]` set to spring, summer, autumn and winter, all on the same engine. The architecture is identical in all four, which is what makes the series read as one project. For several cameras in one season use `set_chain`: the Set node scores the views against each other, but it only corrects numbers, not style, so keep one engine for the whole set.
6. **Weather in motion.** Start from the Accepted still. Weather pass, Vegetation breeze and Water study are locked-tripod motion recipes (the camera does not move); Time of day (two stills) joins two Accepted stills of one camera, say the day take and its dusk take (artist manual section 12).

## 4. What the gates will tell you

- A weather take leaves every locked pixel as it was, so the material ΔE checks should read zero; if they do not, the feather or the mask is wrong, not the weather.
- `invention_flags` fires if the engine drew a new long edge across a planar surface (a wet-ground horizon line, a roof edge). That is a real failure, not noise.
- A take can pass every gate and still look wrong: a heavy-rain sky over hard-shadowed stone is the tell. The gates measure the render's integrity, not the plausibility of the whole picture. That is your scrub.

## 5. What not to do

- Do not override a locked ID or loosen the Mask to get rain on the paving. There is no override for it.
- Do not run a third generation to "fix" a weather take. Depth is two from the plate; render the look instead.
- Do not re-photograph a hero wide to get a mood. The router will tell you, and it is right.
- Do not send a season set from two engines and call it consistent.

## 6. Afterwards

Note the packet, the recipe, the token values and the engine in the project notes. A recipe you ran more than twice is worth keeping: **Save as recipe…** in the Edit editor, scope studio, with the token defaults you used.
