const currentButton = document.querySelector("#archive-current");
const batchButton = document.querySelector("#archive-batch");
const batchUrls = document.querySelector("#batch-urls");
const ownerUserId = document.querySelector("#owner-user-id");
const spaceId = document.querySelector("#space-id");
const visibility = document.querySelector("#visibility");
const visibleTeamIds = document.querySelector("#visible-team-ids");
const statusBox = document.querySelector("#status");

function setBusy(isBusy) {
  currentButton.disabled = isBusy;
  batchButton.disabled = isBusy;
}

function setStatus(message) {
  statusBox.textContent = message;
}

async function send(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response?.ok) throw new Error(response?.error || "Archive failed.");
  return response.result;
}

function settingsPayload() {
  return {
    owner_user_id: ownerUserId.value.trim() || "user_primary",
    space_id: spaceId.value.trim() || "private_primary",
    visibility: visibility.value || "private",
    visible_team_ids: visibleTeamIds.value.split(",").map((item) => item.trim()).filter(Boolean)
  };
}

async function loadSettings() {
  const settings = await chrome.storage.local.get({
    owner_user_id: "user_primary",
    space_id: "private_primary",
    visibility: "private",
    visible_team_ids: []
  });
  ownerUserId.value = settings.owner_user_id;
  spaceId.value = settings.space_id;
  visibility.value = settings.visibility;
  visibleTeamIds.value = (settings.visible_team_ids || []).join(",");
}

async function saveSettings() {
  await chrome.storage.local.set(settingsPayload());
}

async function archiveCurrentTweet() {
  setBusy(true);
  setStatus("Archiving current Tweet...");
  try {
    await saveSettings();
    const result = await send({ type: "PSKA_ARCHIVE_CURRENT", pska: settingsPayload() });
    setStatus(`Saved ${result.id}\nDownloads/${result.path}/`);
  } catch (error) {
    setStatus(error.message || String(error));
  } finally {
    setBusy(false);
  }
}

currentButton.addEventListener("click", archiveCurrentTweet);

batchButton.addEventListener("click", async () => {
  const urls = batchUrls.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (!urls.length) {
    setStatus("Paste at least one URL.");
    return;
  }

  setBusy(true);
  setStatus(`Archiving ${urls.length} URLs...`);
  try {
    await saveSettings();
    const report = await send({ type: "PSKA_ARCHIVE_BATCH", urls, pska: settingsPayload() });
    setStatus(`Done\nSuccess: ${report.success.length}\nFailed: ${report.failed.length}\nDownloads/twitter_archive/batch_report.zip`);
  } catch (error) {
    setStatus(error.message || String(error));
  } finally {
    setBusy(false);
  }
});

loadSettings().then(archiveCurrentTweet);
