# Privacy Policy

Last updated: 2026-10-15

This app ("Terraria Shorts Uploader") is a private, single-user tool. Its only
user is the owner of this repository, who uses it to upload videos they made
to their own YouTube channel.

## What it accesses

With the owner's permission, the app uses the YouTube Data API to:

- upload videos to the owner's own channel (`youtube.upload`)
- read that channel's subscriber count and the view, like and comment counts
  of its own videos (`youtube.readonly`)

## What it stores

- An OAuth refresh token for the owner's channel, kept as an encrypted GitHub
  Actions secret and in a local configuration file on the owner's computer.
- The IDs of the videos it uploaded.

It does not collect, store or share any data about other people, viewers or
other YouTube users, and it does not sell or transfer any data to anyone.

## Revoking access

Access can be removed at any time at https://myaccount.google.com/permissions.
The app's use of YouTube data is also subject to the
[Google Privacy Policy](https://policies.google.com/privacy) and the
[YouTube Terms of Service](https://www.youtube.com/t/terms).

## Contact

Open an issue at https://github.com/brandoncardamone/short-form-content-pipeline/issues
