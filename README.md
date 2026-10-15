# bcdu

A keyboard-first terminal storage explorer for **bcachefs**. Browse with the arrow
keys, inspect allocation and storage policies, and see compression, erasure coding,
durability, degraded data, and devices in one place.

Implementation in progress. The first release will include a synthetic demo,
read-only browsing, a filesystem dashboard, and a confirmed file/directory policy
editor. Filesystem-wide physical accounting is kept distinct from pathname totals.

## License

MIT. This is an independent Python application using filesystem interfaces and the
separately installed `bcachefs` executable. No bcachefs implementation is copied,
linked, or distributed here. bcachefs and bcachefs-tools retain their own licenses;
see the [GNU GPL FAQ on separate programs](https://www.gnu.org/licenses/gpl-faq.html#MereAggregation).
