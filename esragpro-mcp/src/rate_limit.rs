//! Rate Limiting (WP7)
//!
//! Per-subject rate limiting and concurrent request limiting.

#![allow(dead_code)]

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::RwLock;
use tracing::warn;

/// Rate limiter configuration
#[derive(Debug, Clone)]
pub struct RateLimitConfig {
    /// Requests per minute per subject
    pub rpm: u32,
    /// Max concurrent requests per subject
    pub concurrent: u32,
}

impl Default for RateLimitConfig {
    fn default() -> Self {
        Self {
            rpm: 60,
            concurrent: 10,
        }
    }
}

/// Rate limit entry for a subject
struct SubjectState {
    /// Request timestamps in the current window
    timestamps: Vec<Instant>,
    /// Current concurrent requests
    concurrent: u32,
    /// Last cleanup time
    last_cleanup: Instant,
}

impl SubjectState {
    fn new() -> Self {
        Self {
            timestamps: Vec::new(),
            concurrent: 0,
            last_cleanup: Instant::now(),
        }
    }

    /// Check if rate limit exceeded
    fn is_rate_limited(&self, rpm: u32) -> bool {
        let window = Duration::from_secs(60);
        let now = Instant::now();
        let count = self
            .timestamps
            .iter()
            .filter(|&t| now.duration_since(*t) < window)
            .count();
        count >= rpm as usize
    }

    /// Check if concurrent limit exceeded
    fn is_concurrent_limited(&self, limit: u32) -> bool {
        self.concurrent >= limit
    }

    /// Record a request
    fn record_request(&mut self) {
        self.timestamps.push(Instant::now());
        self.concurrent += 1;
    }

    /// Complete a request
    fn complete_request(&mut self) {
        if self.concurrent > 0 {
            self.concurrent -= 1;
        }
    }

    /// Cleanup old timestamps
    fn cleanup(&mut self) {
        let window = Duration::from_secs(60);
        let now = Instant::now();
        self.timestamps.retain(|&t| now.duration_since(t) < window);
        self.last_cleanup = now;
    }
}

/// Rate limiter
pub struct RateLimiter {
    config: RateLimitConfig,
    states: Arc<RwLock<HashMap<String, SubjectState>>>,
}

impl RateLimiter {
    /// Create new rate limiter
    pub fn new(config: RateLimitConfig) -> Self {
        Self {
            config,
            states: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// Check if request is allowed
    /// Returns Ok(()) if allowed, Err with retry-after seconds if limited
    pub async fn check(&self, subject_id: &str) -> Result<(), u64> {
        let mut states = self.states.write().await;

        let state = states
            .entry(subject_id.to_string())
            .or_insert_with(SubjectState::new);

        // Cleanup periodically
        if state.last_cleanup.elapsed() > Duration::from_secs(30) {
            state.cleanup();
        }

        // Check concurrent limit first (more critical)
        if state.is_concurrent_limited(self.config.concurrent) {
            warn!(
                subject = %subject_id,
                concurrent = state.concurrent,
                limit = self.config.concurrent,
                "Concurrent rate limit exceeded"
            );
            return Err(1); // Retry after 1 second
        }

        // Check rate limit
        if state.is_rate_limited(self.config.rpm) {
            warn!(
                subject = %subject_id,
                requests = state.timestamps.len(),
                limit = self.config.rpm,
                "Rate limit exceeded"
            );
            return Err(60); // Retry after 60 seconds
        }

        Ok(())
    }

    /// Record a request start
    pub async fn start_request(&self, subject_id: &str) {
        let mut states = self.states.write().await;
        let state = states
            .entry(subject_id.to_string())
            .or_insert_with(SubjectState::new);
        state.record_request();
    }

    /// Record a request completion
    pub async fn complete_request(&self, subject_id: &str) {
        let mut states = self.states.write().await;
        if let Some(state) = states.get_mut(subject_id) {
            state.complete_request();
        }
    }

    /// Get current stats for monitoring
    pub async fn stats(&self) -> HashMap<String, (usize, u32)> {
        let states = self.states.read().await;
        states
            .iter()
            .map(|(k, v)| (k.clone(), (v.timestamps.len(), v.concurrent)))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_rate_limiter_allows_under_limit() {
        let config = RateLimitConfig {
            rpm: 10,
            concurrent: 5,
        };
        let limiter = RateLimiter::new(config);

        for _ in 0..5 {
            assert!(limiter.check("user-1").await.is_ok());
            limiter.start_request("user-1").await;
        }
    }

    #[tokio::test]
    async fn test_rate_limiter_blocks_over_limit() {
        let config = RateLimitConfig {
            rpm: 2,
            concurrent: 2,
        };
        let limiter = RateLimiter::new(config);

        // First two should pass
        assert!(limiter.check("user-1").await.is_ok());
        limiter.start_request("user-1").await;

        assert!(limiter.check("user-1").await.is_ok());
        limiter.start_request("user-1").await;

        // Third should fail (concurrent)
        assert!(limiter.check("user-1").await.is_err());
    }

    #[tokio::test]
    async fn test_rate_limiter_release() {
        let config = RateLimitConfig {
            rpm: 10,
            concurrent: 1,
        };
        let limiter = RateLimiter::new(config);

        assert!(limiter.check("user-1").await.is_ok());
        limiter.start_request("user-1").await;

        // Should be limited now
        assert!(limiter.check("user-1").await.is_err());

        // Complete request
        limiter.complete_request("user-1").await;

        // Should be allowed again
        assert!(limiter.check("user-1").await.is_ok());
    }
}
