#!/bin/bash
# run_all.sh OUTDIR: build (annotated, byte-identical check), profile and report the 8-game set.
set -e
O=${1:-/tmp/cb}; mkdir -p $O; D=$(cd "$(dirname "$0")" && pwd)
R=/workspace/roms-compat; N="start@60:5,right@300:400,a@400:10,a@500:10"
for g in z:$R/44e33dc0a6fb.nes c:$R/contra.nes m:$R/6f74e8a7575a.nes v:$R/castlevania.nes smb:/workspace/roms/smb.nes dk:/workspace/roms/dk.nes ic:/workspace/roms/ic.nes bf:/workspace/roms/bf.nes; do
  $D/build.sh ${g#*:} $O/${g%%:*}
done
P="python3 $D/profile_run.py"
$P $O/z.gbc $R/44e33dc0a6fb.nes 15 zelda 60 360 $O/z.npy &
$P $O/c.gbc $R/contra.nes 1a "start@300:5,right@800:700,b@850:5,b@900:5,b@950:5,b@1000:5,a@1100:5" 700 1600 $O/c.npy &
$P $O/m.gbc $R/6f74e8a7575a.nes 23 "start@200:5,start@800:5,right@1500:3000,a@1700:20,a@1900:20,a@2100:20,b@1600:5,b@1800:5" 1600 2600 $O/m.npy &
$P $O/v.gbc $R/castlevania.nes 1a "start@150:5,right@600:1600,b@900:5,b@1100:5,b@1300:5" 700 1700 $O/v.npy &
for g in smb dk ic bf; do $P $O/$g.gbc /workspace/roms/$g.nes nmi "$N" 200 800 $O/$g.npy & done
wait
for g in z c m v smb dk ic bf; do python3 $D/breakdown.py $O/$g $g; done
python3 $D/report.py $O z:Zelda c:Contra v:Castlevania m:MegaMan smb:SMB dk:DK ic:IC bf:BF > $O/report.md
