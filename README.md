# Un-Interupted

A self-hosted podcast proxy that downloads episodes from their official feeds,
removes the ads, and publishes a private, ad-free RSS feed your podcast app can
subscribe to.

**Status: planning.** No application code has been built yet. The build plan and
open questions are in [PLAN.md](PLAN.md).

The ad-removal engine is the existing `ad-remover` skill from
`~/Documents/Projects/RemoveAdds` (whisper.cpp transcription + repeat/sponsor
detection + ffmpeg cut + QA).
