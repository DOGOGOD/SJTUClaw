# Sunny look mechanics

Sunny is a soft, separate-head plush bear. The feet and lower torso stay registered to one baseline. The gaze is led by the dark oval eyes, muzzle, nose, and a small head turn or pitch; the upper torso follows only slightly. Rounded ears and cheek fur follow the head as one soft stuffed form. The centered white belly wrap and green ruffle remain attached to the torso and may compress subtly, but never slide, flip sides, or become a new prop.

Motion budget: every 22.5-degree step moves the eyes, nose, muzzle, head pitch/yaw, and upper torso by a small, even amount. Head scale, feet, lower-body anchor, belly-wrap placement, and overall volume remain stable. No whole-sprite rotation, affine tilt, or pupil-only shortcut.

- 000 up: chin and muzzle lift, pupils and nose read above the head center, lower face foreshortens slightly; both ears remain visible and the torso stays front-oriented.
- 090 screen-right: pupils, nose tip, muzzle, and head face screen-right; the bear's screen-left cheek/ear becomes more visible and the far screen-right cheek compresses slightly.
- 180 down: chin and muzzle tuck toward the chest, eyes look down with reshaped upper lids, nose reads below the head center; the crown and ears become slightly more visible.
- 270 screen-left: pupils, nose tip, muzzle, and head face screen-left; the bear's screen-right cheek/ear becomes more visible and the far screen-left cheek compresses slightly.

Diagonals interpolate evenly between these four pose families. The 157.5-to-180 and 337.5-to-000 boundaries must be one ordinary step with no snap, scale pop, or side flip.
