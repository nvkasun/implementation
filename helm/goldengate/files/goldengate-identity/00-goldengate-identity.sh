# Sourced by the image entrypoint (deployment-main.sh) from ${OGG_DEPLOYMENT_SCRIPTS}/setup, before Service Manager and NGINX start.
# Installs a replace-variables.sh function so NGINX routes are rendered with the deployment actually registered in Service Manager.

goldengate_identity_abort() {
	echo "goldengate-identity: ERROR: $*" >&2
	exit 1
}

GOLDENGATE_IDENTITY_HOME="${GOLDENGATE_IDENTITY_HOME:-/opt/goldengate-identity}"
GOLDENGATE_IDENTITY_ENTRYPOINT="${BASH_SOURCE[${#BASH_SOURCE[@]}-1]}"

[ -f "${GOLDENGATE_IDENTITY_HOME}/resolve_deployment.py" ] || goldengate_identity_abort "resolver not found in ${GOLDENGATE_IDENTITY_HOME}"
command -v python3 >/dev/null 2>&1 || goldengate_identity_abort "python3 is not available"
command -v replace-variables.sh >/dev/null 2>&1 || goldengate_identity_abort "replace-variables.sh is not on PATH"
grep -Eq '^[[:space:]]*replace-variables\.sh[[:space:]]' "${GOLDENGATE_IDENTITY_ENTRYPOINT}" ||
	goldengate_identity_abort "${GOLDENGATE_IDENTITY_ENTRYPOINT} does not render NGINX through a bare replace-variables.sh call"

if [ -d "${OGG_DEPLOYMENT_HOME:?}/Deployment/etc" ]; then
	GOLDENGATE_IDENTITY_FRESH_VOLUME=false
else
	GOLDENGATE_IDENTITY_FRESH_VOLUME=true
fi
export GOLDENGATE_IDENTITY_HOME GOLDENGATE_IDENTITY_FRESH_VOLUME
unset GOLDENGATE_IDENTITY_RESOLVED

replace-variables.sh() {
	local resolved
	if ! resolved="$(python3 "${GOLDENGATE_IDENTITY_HOME}/resolve_deployment.py" resolve \
		--bootstrap-name "${OGG_DEPLOYMENT:-}" --fresh-volume "${GOLDENGATE_IDENTITY_FRESH_VOLUME}")"; then
		goldengate_identity_abort "registered deployment could not be resolved; NGINX routes were not rendered"
	fi
	export OGG_DEPLOYMENT="${resolved}"
	export GOLDENGATE_IDENTITY_RESOLVED="${resolved}"
	command replace-variables.sh "$@"
}

[ "$(type -t replace-variables.sh)" = "function" ] || goldengate_identity_abort "the shell did not accept the replace-variables.sh function"
echo "goldengate-identity: setup complete (bootstrap name ${OGG_DEPLOYMENT:-<unset>}, fresh volume ${GOLDENGATE_IDENTITY_FRESH_VOLUME})"
