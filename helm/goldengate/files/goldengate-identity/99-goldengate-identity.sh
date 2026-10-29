# Sourced by the image entrypoint from ${OGG_DEPLOYMENT_SCRIPTS}/startup, after NGINX starts.
# Stops the container if NGINX was rendered without the registered deployment name.

if [ -z "${GOLDENGATE_IDENTITY_RESOLVED:-}" ]; then
	echo "goldengate-identity: ERROR: NGINX was rendered without registered-deployment resolution (setup hook missing or bypassed)" >&2
	exit 1
fi
python3 "${GOLDENGATE_IDENTITY_HOME:-/opt/goldengate-identity}/resolve_deployment.py" verify-nginx \
	--conf-glob "${GOLDENGATE_IDENTITY_NGINX_CONF_GLOB:-/etc/nginx/*.conf}" --expected "${GOLDENGATE_IDENTITY_RESOLVED}" || exit 1
