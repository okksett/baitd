# Bundled fonts: sources

All fonts are static (non-variable) TrueType files under the SIL Open Font License 1.1.
Downloaded 2026-11-07. Each font's licence text sits next to this file.

| File | Version | Exact URL | Licence file |
|---|---|---|---|
| Anton-Regular.ttf | 2.116 | https://raw.githubusercontent.com/google/fonts/main/ofl/anton/Anton-Regular.ttf | Anton-OFL.txt (https://raw.githubusercontent.com/google/fonts/main/ofl/anton/OFL.txt) |
| InstrumentSerif-Italic.ttf | 1.000 | https://raw.githubusercontent.com/google/fonts/main/ofl/instrumentserif/InstrumentSerif-Italic.ttf | InstrumentSerif-OFL.txt (https://raw.githubusercontent.com/google/fonts/main/ofl/instrumentserif/OFL.txt) |
| Inter-Regular.ttf, Inter-SemiBold.ttf | 4.001 (release v4.1, 2024-11-16) | extras/ttf/ inside https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip | Inter-OFL.txt (LICENSE.txt from the same zip) |
| Montserrat-ExtraBold.ttf | 9.000 | https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-ExtraBold.ttf | Montserrat-OFL.txt (https://raw.githubusercontent.com/JulietaUla/Montserrat/master/OFL.txt) |
| NotoSans-Regular.ttf | 2.015 | https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/fonts/NotoSans/full/ttf/NotoSans-Regular.ttf | NotoSans-OFL.txt (https://raw.githubusercontent.com/google/fonts/main/ofl/notosans/OFL.txt) |

Notes
- google/fonts only ships variable Inter, Montserrat and Noto Sans, so those three come from their upstream static builds.
- Noto Sans covers Latin, Greek and Cyrillic, not CJK. For CJK/Arabic/etc. libass falls back to a system font.
