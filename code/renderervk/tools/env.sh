# env.sh -- settings shared by the comparison scripts (sourced, not run)
#
# MOH_BASEPATH  game data (read only), default ~/Documents/moh
# MOH_HOMEPATH  throwaway fs_homepath, default /tmp/moh-vktest
# MOH_BINDIR    directory of the openmohaa binary, default build-vk/RelWithDebInfo
# MOH_OUT       logs, PNG copies and diffs, default $MOH_HOMEPATH/compare
TOOLS=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$TOOLS/../../.." && pwd)
MOH_BASEPATH=${MOH_BASEPATH:-$HOME/Documents/moh}
MOH_HOMEPATH=${MOH_HOMEPATH:-/tmp/moh-vktest}
MOH_BINDIR=${MOH_BINDIR:-$REPO/build-vk/RelWithDebInfo}
MOH_OUT=${MOH_OUT:-$MOH_HOMEPATH/compare}
export MOH_BASEPATH MOH_HOMEPATH MOH_BINDIR MOH_OUT
mkdir -p "$MOH_HOMEPATH/main" "$MOH_OUT"
