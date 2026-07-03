!(import! &self (library OmegaClaw-Core ./profile/policy.py))

(= (securityPolicyPath) (empty))

(= (applySecurityPolicy)
   (progn (py-call (logger.log_info "Configuring security policy" "policy"))
          (configure securityPolicyPath "./repos/OmegaClaw-Core/profile/policy.yaml")
          (py-call (policy.apply_security_policy (securityPolicyPath)))
          True))
