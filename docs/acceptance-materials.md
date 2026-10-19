# Materials and standalone playback acceptance — issue #3

Date: 19 October 2026. Scope: F2, F8, F7; portions of A2, A3, A10, A12, A14. These results do not claim full MVP completion. Specification section 13.1 supplies the agreed public API test boundary.

| Check | Result | Evidence and remaining work |
| --- | --- | --- |
| Public import/read/profile operations | Passed | `backend/tests/test_materials.py`: SRT and VTT imports reopen after process restart using the same SQLite profile and material IDs. |
| Validation and limits | Passed | API rejects empty/invalid tracks, reversed or unordered times, invalid YouTube hosts, more than 2 MB, 10,000 cues, or 200,000 characters; messages identify the problem. Invalid import leaves the library unchanged. |
| Hash reuse and replacement | Passed | Identical tracks reuse a material; changed track creates version 2 linked to version 1. Previous material and its occurrence IDs remain readable. |
| Repeated overlapping subtitles | Passed | Multiword rolling text and duplicate overlapping cues coalesce, preserving original fragments; identical phrases spoken later remain separate cues. |
| Lexical identities and offsets | Passed | Real pinned spaCy pipeline maps `running` and `ran` to the same English `run/VERB` lexeme. Unicode offsets, source markup, uncertainty fallback, and mixed material/cue/token rejection are checked through the API. |
| Host/origin/request checks | Passed | Local host and configured origin accepted; foreign host/origin, text/plain import, unexpected fields, non-boolean completeness and oversized request rejected. |
| Web build and typecheck | Passed | `npm run build --prefix apps/web` runs strict TypeScript followed by Vite production build. Full integrated baseline suite: 26 tests passed in 4.48 seconds; strict mypy passed for 11 application files and Ruff passed for backend. |
| Prepared video source | Passed for source and public watch playback | 85 licensed English cues, attribution in `data/demo/README.md`. Available in-app browser loaded the verified Blender upload and played to 4.58 seconds, duration 654.381 seconds. |
| Embedded playback, pause, seek, selection | Pending browser acceptance | Player uses the real IFrame API clock, polling every 100 ms. Observe actual cue transitions, selection pause, explicit continuation and embedding denial in the running app. The implementation interval alone does not prove the 0.5 second target. |
| Chrome without extension | Pending | Chrome is unavailable to the agent's enabled browser surfaces. The available in-app browser does not establish the required actual Chrome result. |
| Container recreation | Pending integrated Compose run | Persistent named SQLite volume and schema version/backup support provided. Process restart is covered by API tests; container recreation must be verified separately. |
| AI card, vocabulary, diagnosis, chat | Outside issue #3 | Dependency tickets implement these features; the selected occurrence contract is available now. A12/A14 remain incomplete until the full web scenario passes. |

Manual browser procedure: start Compose, open the prepared material, press play and seek to 0:15/0:28/4:26; compare the active subtitle to audio and the visible clock. Click a word during playback and while already paused; record the material, cue, token and source sentence. Close selection and confirm no automatic playback. Use explicit continuation, seek backward/forward, replace a track and reopen the older version, navigate between two imported materials, and confirm no old selection appears. Test a video known to prohibit embedding in the test region; its text must remain readable with a YouTube link. Repeat in actual Chrome and record timing, browser version, environment and any failure.

Manual persistence procedure: add a unique SRT/VTT material, record its ID, run `docker compose down` followed by `docker compose up -d`, reopen the material and verify the same ID and tokens. Do not remove volumes. Tests for later familiarity features must additionally verify word scores survive the same operation.
