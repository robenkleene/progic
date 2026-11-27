# Logic Test Projects

Open each `.logicx` in Logic Pro. Each test isolates one write path, so a failure points at one feature. `<name>.json` is the edit file that made it; `<name>.expected.txt` is what `progic inspect` reads back.

For every project, first check: it opens without an error or "damaged project" warning, and save + reopen works.

| Project                     | Check in Logic                                                                                                                                                                          |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `T01_tempo_meter`           | Tempo 138 at bar 1, 140 at bar 5, 128.5 at bar 9 beat 3. Signatures 4/4, 3/4 at bar 5, 7/8 at bar 9. LCD shows 138 / 4/4.                                                               |
| `T02_markers`               | Markers Intro (bar 1), Breakdown (9), "Drop & Build" (17 beat 3), Outro (33). Old MARKERONE..THREE gone.                                                                                |
| `T03_rename_mixer`          | Tracks named Lead, Bass, "Pad Ünïcode", "A much longer track name than before". Mixer names match. Lead fader above 0 dB, Bass fader lower and panned hard left, Pad panned hard right. |
| `T04_region_from_prototype` | No existing regions in the source, so this uses the bundled prototype. One region bars 2-3, 16-note A minor arpeggio (A2 C3 E3 A3 in Logic's naming). It plays the track's instrument.  |
| `T05_region_cloned`         | Original 3-note region at bar 1 untouched. "Arp" at bar 3 (8 notes), "Offbeat" at bar 5 beat 3, 2 bars long, one held chord.                                                            |
| `T06_clear_regions`         | Inst 1: old region gone, one 4-bar arpeggio region. Inst 2: no regions. Inst 3 and 4: original empty regions still there.                                                               |
| `T07_full_song`             | 138 BPM, 4 markers, tracks Lead/Bass/Pad/Spare + 3 audio tracks. Lead arp bars 17-24, offbeat bass bars 9-24, pad chords bars 1-8 and 17-24, Spare empty. Playback works.               |

Note names: progic uses C4 = 60, Logic shows that note as C3.

Useful extra checks if something looks off: piano roll velocities vary (not all 100); regions can be moved, copied, and edited; undo works.
