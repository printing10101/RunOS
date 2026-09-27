<template>
  <!-- 修仙山水背景：层叠青绿山影 + 九重巨塔 + 浮空岛 + 冷月云雾。
       纯 SVG 手绘（无版权依赖），fixed 垫底、不响应指针，暗色下刻意低对比不抢 UI。 -->
  <div class="backdrop" aria-hidden="true">
    <svg viewBox="0 0 1600 1000" preserveAspectRatio="xMidYMid slice">
      <defs>
        <linearGradient id="xb-sky" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#050d0a" />
          <stop offset="0.55" stop-color="#0a1c16" />
          <stop offset="1" stop-color="#0e241c" />
        </linearGradient>
        <radialGradient id="xb-moon-halo">
          <stop offset="0" stop-color="#d7ead9" stop-opacity="0.16" />
          <stop offset="0.5" stop-color="#d7ead9" stop-opacity="0.05" />
          <stop offset="1" stop-color="#d7ead9" stop-opacity="0" />
        </radialGradient>
        <linearGradient id="xb-mtn1" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#133129" />
          <stop offset="1" stop-color="#0e251e" />
        </linearGradient>
        <linearGradient id="xb-mtn2" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#0d211a" />
          <stop offset="1" stop-color="#091b15" />
        </linearGradient>
        <linearGradient id="xb-mtn3" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#050f0a" />
          <stop offset="1" stop-color="#03100b" />
        </linearGradient>
        <linearGradient id="xb-veil" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#040a08" stop-opacity="0.22" />
          <stop offset="0.45" stop-color="#040a08" stop-opacity="0" />
          <stop offset="1" stop-color="#040a08" stop-opacity="0.3" />
        </linearGradient>
        <linearGradient id="xb-river" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="#7fc8b2" stop-opacity="0" />
          <stop offset="0.5" stop-color="#7fc8b2" stop-opacity="0.5" />
          <stop offset="1" stop-color="#7fc8b2" stop-opacity="0.1" />
        </linearGradient>
        <filter id="xb-blur" x="-40%" y="-200%" width="180%" height="500%">
          <feGaussianBlur stdDeviation="16" />
        </filter>
      </defs>

      <!-- 天空与星月 -->
      <rect width="1600" height="1000" fill="url(#xb-sky)" />
      <g fill="#cfe8db">
        <circle cx="180" cy="120" r="1.2" opacity="0.4" />
        <circle cx="340" cy="220" r="0.9" opacity="0.25" />
        <circle cx="520" cy="90" r="1.1" opacity="0.35" />
        <circle cx="640" cy="180" r="0.8" opacity="0.2" />
        <circle cx="820" cy="70" r="1.3" opacity="0.4" />
        <circle cx="960" cy="160" r="0.9" opacity="0.3" />
        <circle cx="1030" cy="60" r="1" opacity="0.25" />
        <circle cx="1330" cy="90" r="1.1" opacity="0.35" />
        <circle cx="1460" cy="210" r="0.9" opacity="0.3" />
        <circle cx="1530" cy="120" r="1.2" opacity="0.4" />
        <circle cx="90" cy="300" r="0.9" opacity="0.2" />
        <circle cx="450" cy="300" r="0.8" opacity="0.22" />
        <circle cx="760" cy="260" r="1" opacity="0.28" />
        <circle cx="1390" cy="320" r="0.9" opacity="0.24" />
      </g>
      <circle cx="1180" cy="180" r="210" fill="url(#xb-moon-halo)" />
      <circle cx="1180" cy="180" r="62" fill="#d7ead9" opacity="0.78" />
      <circle cx="1160" cy="164" r="9" fill="#bcd8c6" opacity="0.3" />
      <circle cx="1198" cy="196" r="6" fill="#bcd8c6" opacity="0.24" />

      <!-- 远山两叠 -->
      <path d="M -20 640 L 120 540 L 260 600 L 420 470 L 560 560 L 700 500 L 840 590 L 980 520 L 1120 610 L 1280 540 L 1430 620 L 1620 560 L 1620 1010 L -20 1010 Z"
            fill="url(#xb-mtn1)" opacity="0.85" />
      <ellipse cx="420" cy="656" rx="520" ry="38" fill="#9fd8c4" opacity="0.07" filter="url(#xb-blur)" />
      <path d="M -20 700 L 100 620 L 240 676 L 380 560 L 520 646 L 660 588 L 800 668 L 940 600 L 1090 680 L 1240 610 L 1390 690 L 1620 620 L 1620 1010 L -20 1010 Z"
            fill="url(#xb-mtn2)" opacity="0.95" />
      <ellipse cx="1120" cy="700" rx="600" ry="44" fill="#9fd8c4" opacity="0.08" filter="url(#xb-blur)" />

      <!-- 浮空岛两座：岩体下垂，上负小庙（塔两侧各一，避让塔身） -->
      <g transform="translate(-240, -60)">
        <path d="M 700 402 Q 718 380 782 384 Q 842 388 840 408 Q 838 432 800 436 L 786 470 Q 770 452 756 472 Q 740 448 710 442 Q 692 420 700 402 Z"
              fill="#0a1c15" />
        <path d="M 748 384 L 748 362 L 744 362 L 760 350 L 776 362 L 772 362 L 772 384 Z" fill="#030c08" />
        <rect x="754" y="368" width="5" height="7" fill="#d9a24e" opacity="0.6" />
        <ellipse cx="770" cy="480" rx="90" ry="14" fill="#9fd8c4" opacity="0.08" filter="url(#xb-blur)" />
      </g>
      <g transform="translate(-120, 20)">
        <path d="M 580 540 Q 594 524 638 527 Q 682 530 680 545 Q 678 562 652 566 L 642 590 Q 630 576 620 592 Q 608 572 588 568 Q 574 554 580 540 Z"
              fill="#081711" />
        <path d="M 618 527 L 618 512 L 615 512 L 627 503 L 639 512 L 636 512 L 636 527 Z" fill="#030c08" />
        <ellipse cx="630" cy="600" rx="66" ry="10" fill="#9fd8c4" opacity="0.06" filter="url(#xb-blur)" />
      </g>

      <!-- 九重巨塔：飞檐收分，下层没入前山，唯上层窗火点点（偏左下，避开常驻卡片区） -->
      <g fill="#04100b" transform="translate(-390, -10)">
        <!-- 塔身（每层：宽檐 + 收分body，自上而下渐宽） -->
        <rect x="1147" y="106" width="6" height="80" />
        <circle cx="1150" cy="102" r="6" fill="#d4b06a" opacity="0.45" />
        <path d="M 1104 200 Q 1127 214 1150 214 Q 1173 214 1196 200 Q 1180 186 1178 180 L 1122 180 Q 1120 186 1104 200 Z" />
        <rect x="1120" y="214" width="60" height="58" />
        <path d="M 1092 286 Q 1121 302 1150 302 Q 1179 302 1208 286 Q 1190 270 1188 264 L 1112 264 Q 1110 270 1092 286 Z" />
        <rect x="1108" y="302" width="84" height="56" />
        <path d="M 1078 372 Q 1114 390 1150 390 Q 1186 390 1222 372 Q 1202 354 1200 348 L 1100 348 Q 1098 354 1078 372 Z" />
        <rect x="1094" y="390" width="112" height="54" />
        <path d="M 1062 458 Q 1106 478 1150 478 Q 1194 478 1238 458 Q 1216 438 1214 432 L 1086 432 Q 1084 438 1062 458 Z" />
        <rect x="1080" y="478" width="140" height="52" />
        <path d="M 1046 544 Q 1098 566 1150 566 Q 1202 566 1254 544 Q 1230 522 1228 516 L 1072 516 Q 1070 522 1046 544 Z" />
        <rect x="1064" y="566" width="172" height="50" />
        <path d="M 1028 630 Q 1089 654 1150 654 Q 1211 654 1272 630 Q 1246 606 1244 600 L 1056 600 Q 1054 606 1028 630 Z" />
        <rect x="1048" y="654" width="204" height="48" />
        <path d="M 1008 716 Q 1080 742 1150 742 Q 1220 742 1292 716 Q 1264 690 1262 684 L 1038 684 Q 1036 690 1008 716 Z" />
        <rect x="1030" y="742" width="240" height="46" />
        <path d="M 986 802 Q 1068 830 1150 830 Q 1232 830 1314 802 Q 1284 774 1282 768 L 1018 768 Q 1016 774 986 802 Z" />
        <rect x="1010" y="830" width="280" height="44" />
        <path d="M 962 888 Q 1064 918 1150 918 Q 1236 918 1338 888 Q 1306 858 1304 852 L 996 852 Q 994 858 962 888 Z" />
      </g>
      <!-- 点金窗：上层五层，明暗不一 -->
      <g fill="#d9a24e" transform="translate(-390, -10)">
        <rect x="1136" y="236" width="7" height="10" opacity="0.65" />
        <rect x="1146" y="236" width="7" height="10" opacity="0.4" />
        <rect x="1156" y="236" width="7" height="10" opacity="0.55" />
        <rect x="1130" y="324" width="7" height="10" opacity="0.5" />
        <rect x="1146" y="324" width="7" height="10" opacity="0.7" />
        <rect x="1162" y="324" width="7" height="10" opacity="0.45" />
        <rect x="1124" y="412" width="7" height="10" opacity="0.6" />
        <rect x="1146" y="412" width="7" height="10" opacity="0.35" />
        <rect x="1168" y="412" width="7" height="10" opacity="0.6" />
        <rect x="1118" y="500" width="7" height="10" opacity="0.5" />
        <rect x="1146" y="500" width="7" height="10" opacity="0.65" />
        <rect x="1174" y="500" width="7" height="10" opacity="0.4" />
        <rect x="1110" y="588" width="7" height="10" opacity="0.55" />
        <rect x="1146" y="588" width="7" height="10" opacity="0.4" />
        <rect x="1182" y="588" width="7" height="10" opacity="0.6" />
      </g>
      <!-- 塔尖青辉 -->
      <circle cx="760" cy="150" r="120" fill="#3fd0a4" opacity="0.05" filter="url(#xb-blur)" />

      <!-- 高层云雾带 -->
      <ellipse cx="900" cy="430" rx="380" ry="26" fill="#9fd8c4" opacity="0.06" filter="url(#xb-blur)" />
      <ellipse cx="1250" cy="560" rx="330" ry="24" fill="#9fd8c4" opacity="0.07" filter="url(#xb-blur)" />

      <!-- 飞鸟三两 -->
      <g stroke="#9cc4b4" stroke-width="2" fill="none" stroke-linecap="round" opacity="0.5">
        <path d="M 862 302 q 9 -9 18 0 q 9 -9 18 0" />
        <path d="M 918 282 q 7 -7 14 0 q 7 -7 14 0" />
        <path d="M 828 268 q 6 -6 12 0 q 6 -6 12 0" />
      </g>

      <!-- 前山：吞没塔基，反衬巨构 -->
      <path d="M -20 820 L 150 740 L 320 800 L 480 716 L 650 788 L 820 730 L 990 800 L 1160 744 L 1330 812 L 1500 760 L 1620 800 L 1620 1010 L -20 1010 Z"
            fill="url(#xb-mtn3)" />
      <!-- 月照江带 -->
      <path d="M 180 980 C 420 900 520 860 760 830 C 980 806 1120 770 1240 740"
            stroke="url(#xb-river)" stroke-width="12" fill="none" stroke-linecap="round" opacity="0.22" />
      <!-- 近脊松影 -->
      <g fill="#020a07">
        <path d="M 130 906 l 14 -34 l 14 34 l -9 0 l 12 26 l -34 0 l 12 -26 Z" />
        <path d="M 196 892 l 12 -28 l 12 28 l -8 0 l 10 22 l -28 0 l 10 -22 Z" />
        <path d="M 1350 884 l 15 -36 l 15 36 l -10 0 l 13 28 l -36 0 l 13 -28 Z" />
        <path d="M 1432 902 l 12 -28 l 12 28 l -8 0 l 10 22 l -28 0 l 10 -22 Z" />
      </g>
      <path d="M -20 940 L 200 880 L 420 930 L 700 886 L 980 936 L 1260 892 L 1620 940 L 1620 1010 L -20 1010 Z"
            fill="#030d09" />

      <!-- 上下压暗，保证 UI 可读 -->
      <rect width="1600" height="1000" fill="url(#xb-veil)" />
    </svg>
  </div>
</template>

<style scoped>
.backdrop {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background: var(--bg-page);
}
.backdrop svg { width: 100%; height: 100%; display: block; }
</style>
