# Logic Test Projects: Adding Tracks

All three start from `mixed_template_2inst` (Inst 1, Audio 1, Inst 2, Inst 3). For each: it opens without a warning, save + reopen works, and the added tracks appear in both the Tracks area and the Mixer with working faders.

| Project                       | Check in Logic                                                                                                                                                                                                                                      |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `T08_add_one_track`           | A 5th track "Added" at the bottom. Its instrument slot is empty; loading an instrument on it (e.g. Alchemy) works and it plays from the keyboard when selected.                                                                                     |
| `T09_add_tracks_with_content` | 138 BPM. "Keys" (renamed Inst 1) has a 2-bar arpeggio. Added: "Arp" (fader lower, panned left) with an arpeggio at bars 3-6, "Sub Bass" with 8 held A notes, "Empty Added". Load an instrument on Arp and Sub Bass and the regions play through it. |
| `T10_add_twelve_tracks`       | 12 added tracks, "Added 1" to "Added 12", all visible (not cut off at the bottom), each with its own mixer strip. Adding one more track in Logic afterwards also works.                                                                             |
