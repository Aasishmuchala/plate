# Hosting a review bundle (documented only, nothing here uploads)

A review bundle is a zip the client opens from disk. To hand it over as a link instead, put the unzipped
folder on Cloudflare R2 and share a signed, expiring URL. PLATE does not do this itself: v1 has no
upload code, no credentials and no network call in the exporter or the page (BUILD_PLAN decision 21).

1. Unzip `review-<id>.zip` and upload the `review-<id>/` folder to a private R2 bucket (`rclone` or the
   Cloudflare dashboard). Keep the bucket private.
2. R2 presigned URLs sign one object each, so a folder cannot be shared by one link. Serve the folder
   through a Worker that checks a signed, expiring token on every request (HMAC over path and expiry), or
   zip-and-share (the zip is already one file: sign that single object).
3. Pick the shortest expiry that fits the review (days, not months) and rotate the signing key after the
   job. NDA projects: an R2 bucket is a custody C2 route; use the zip by hand, or a bucket covered by the
   client's terms (see docs/custody.md).
4. The page works over https from R2 as well as from `file://`. Its Content-Security-Policy allows
   no connections, so hosting it adds no tracking and the comment buttons still work offline.
