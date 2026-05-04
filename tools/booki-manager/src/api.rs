use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};
use std::time::Duration;

#[derive(Serialize)]
struct JobRunRequest<'a> {
    kind: &'a str,
    args: Vec<String>,
}

#[derive(Deserialize)]
struct JobRunResponse {
    job_id: String,
}

#[derive(Deserialize, Debug)]
pub struct JobStatus {
    pub status: String,        // pending | running | success | failed
    #[serde(default)]
    pub exit_code: Option<i32>,
    #[serde(default)]
    pub error: String,
    #[serde(default)]
    pub log: String,
}

#[derive(Clone)]
pub struct Client {
    base: String,
}

impl Client {
    pub fn new(base: impl Into<String>) -> Self {
        Self { base: base.into() }
    }

    pub fn health(&self) -> bool {
        ureq::get(&format!("{}/api/health", self.base))
            .timeout(Duration::from_millis(800))
            .call()
            .is_ok()
    }

    /// Ask the running server to shut down cleanly. Used by the tray
    /// menu's Stop / Restart so the manager can stop *adopted* servers
    /// (started outside the manager) — there's no Child handle for
    /// those, so SIGKILL via `Child::kill` doesn't apply.
    pub fn shutdown(&self) -> Result<()> {
        // The server replies OK and *then* exits, so the connection may
        // close mid-response — treat that as success. Short timeout so
        // a hung worker doesn't hold up the menu.
        let res = ureq::post(&format!("{}/api/shutdown", self.base))
            .timeout(Duration::from_secs(2))
            .call();
        match res {
            Ok(_) => Ok(()),
            // Connection closed mid-response is expected during shutdown.
            Err(ureq::Error::Transport(t))
                if matches!(t.kind(),
                    ureq::ErrorKind::Io | ureq::ErrorKind::ConnectionFailed) => Ok(()),
            Err(e) => Err(anyhow!("api shutdown: {}", e)),
        }
    }

    /// Submit a sync job. Returns the job id.
    pub fn submit_sync(&self, source: &str) -> Result<String> {
        self.submit_job("sync", &["--source".into(), source.into()])
    }

    /// Submit any job kind known to the Booki job runner (currently
    /// "sync" or "ingest"). Args follow Booki's CLI flag allowlist.
    pub fn submit_job(&self, kind: &str, args: &[String]) -> Result<String> {
        let body = JobRunRequest { kind, args: args.to_vec() };
        let resp: JobRunResponse = ureq::post(&format!("{}/api/jobs/run", self.base))
            .timeout(Duration::from_secs(5))
            .send_json(&body)?
            .into_json()?;
        Ok(resp.job_id)
    }

    pub fn job_status(&self, id: &str) -> Result<JobStatus> {
        let resp = ureq::get(&format!("{}/api/jobs/{}", self.base, id))
            .timeout(Duration::from_secs(3))
            .call()?;
        Ok(resp.into_json()?)
    }

    /// Poll job until terminal. Caller decides total budget; this just
    /// loops with a fixed 1s interval.
    pub fn await_job(&self, id: &str, budget: Duration) -> Result<JobStatus> {
        let deadline = std::time::Instant::now() + budget;
        loop {
            let s = self.job_status(id)?;
            if s.status == "success" || s.status == "failed" {
                return Ok(s);
            }
            if std::time::Instant::now() >= deadline {
                return Err(anyhow!("timed out waiting for job {}", id));
            }
            std::thread::sleep(Duration::from_secs(1));
        }
    }
}
