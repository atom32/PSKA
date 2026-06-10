const currentButton = document.querySelector("#archive-current");
const batchButton = document.querySelector("#archive-batch");
const batchUrls = document.querySelector("#batch-urls");
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

async function archiveCurrentTweet() {
  setBusy(true);
  setStatus("Archiving current Tweet...");
  try {
    const result = await send({ type: "PSKA_ARCHIVE_CURRENT" });
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
    const report = await send({ type: "PSKA_ARCHIVE_BATCH", urls });
    setStatus(`Done\nSuccess: ${report.success.length}\nFailed: ${report.failed.length}\nDownloads/twitter_archive/batch_report.zip`);
  } catch (error) {
    setStatus(error.message || String(error));
  } finally {
    setBusy(false);
  }
});

archiveCurrentTweet();
