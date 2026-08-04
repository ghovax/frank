How well an element must match, as a cosine against the query, before `find_many` will return it at all. It cuts the noise band and nothing more.

The limit of that is measured rather than assumed. On 596 elements of a real page, six paraphrased queries for things that WERE present scored 0.48 to 0.75 at top-1, while six for things that were plainly absent — a checkout button, a flight time — scored 0.26 to 0.59. Those distributions overlap, so no absolute cosine separates "here" from "not here" with this embedding. Set it where it removes the tail that is unambiguously noise (that page ran to -0.12) without touching the band where real matches live.

Treat an empty result as "nothing scored above the noise", never as proof of absence, and do not raise this hoping to buy absence detection — it would cost real matches first.

That claim has since been tested properly and it holds, which is worth recording because an easier test says otherwise. Against negatives drawn from unrelated screens the cosine looks like it separates present from absent well, at AUC 0.94 — but a query about a periodic table asked of the Finder is not the case a floor exists for. Against the real one — 12,304 queries written for a screen and then asked of that same screen with their own element removed — it is AUC 0.55, and a threshold refusing 90% of them would discard 84% of the answers that were present.

It stays a cosine against the query, deliberately, now that the ranking is a fusion. A fused score is scaled by the query's own length, so absent queries can outscore present ones on it (AUC 0.07) purely by being shorter. A floor has to read a number that means the same thing from one call to the next, and only the plain cosine does.
