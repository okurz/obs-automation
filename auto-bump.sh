#!/bin/bash
set -eo pipefail

PACKAGE="cf-cli"
OBS_PROJECT="home:okurz:branches:Cloud:Tools" # Update to the target project if needed
ANITYA_PROJECT_ID="385503"

echo "Checking Anitya (Release Monitoring) for project ID $ANITYA_PROJECT_ID..."
# Fetch the latest stable tag from Anitya
LATEST_TAG=$(curl -s -L "https://release-monitoring.org/api/project/$ANITYA_PROJECT_ID" | jq -r .version)

if [ -z "$LATEST_TAG" ] || [ "$LATEST_TAG" == "null" ]; then
	echo "Failed to fetch latest tag from Anitya!"
	exit 1
fi

# Ensure the tag is prefixed with 'v' as required by the _service file configuration
if [[ ! "$LATEST_TAG" == v* ]]; then
	LATEST_TAG="v$LATEST_TAG"
fi

echo "Checking out OBS package..."
osc checkout "$OBS_PROJECT" "$PACKAGE"
cd "$OBS_PROJECT/$PACKAGE"

# Extract current tag from the _service file
CURRENT_TAG=$(grep -oP '(?<=<param name="revision">)[^<]+' _service)

echo "Current OBS version: $CURRENT_TAG"
echo "Latest Upstream version (Anitya): $LATEST_TAG"

if [ "$CURRENT_TAG" == "$LATEST_TAG" ]; then
	echo "Package is already up to date. Exiting."
	exit 0
fi

echo "Updating _service file to $LATEST_TAG..."
sed -i "s|<param name=\"revision\">$CURRENT_TAG</param>|<param name=\"revision\">$LATEST_TAG</param>|" _service

echo "Running OBS services locally to generate tarballs and spec updates..."
# This executes tar_scm, recompress, set_version, and go_modules
osc service ra

echo "Cleaning up old tracked files and adding new ones..."
# Automatically removes the old tarball/vendor files and stages the new ones
osc addremove

# Commit to the branch project
echo "Committing to $OBS_PROJECT..."
osc ci -m "Update $PACKAGE to $LATEST_TAG"

# Create a submit request to the target project
echo "Creating Submit Request..."
osc sr -m "Automated update to $LATEST_TAG based on Anitya release monitoring"
