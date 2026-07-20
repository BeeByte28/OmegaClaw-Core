(= (maxNewInputLoops) (empty))
(= (maxWakeLoops) (empty))
(= (sleepInterval) (empty))
(= (LLM) (empty))
(= (provider) (empty))
(= (maxOutputToken) (empty))
(= (reasoningMode) (empty))
(= (wakeupInterval) (empty))
(= (logConfigPath) (empty))
(= (memoryDirectory) (empty))
(= (spamShield) (empty)) ; TODO: this parameter is considered deprecated

(= (initLoop)
   (progn (configure maxNewInputLoops 50) ;20
          (configure maxWakeLoops 1)
          (configure spamShield False)
          (configure sleepInterval 1) ;10
          (configure LLM gpt-5.4)
          (configure provider Anthropic)
          (configure maxOutputToken 6000)
          (configure reasoningMode medium)
          (configure wakeupInterval 600) ;600=10 minutes
          (configure logConfigPath "")
          (configure memoryDirectory ./)
          (change-state! &prevmsg "")
          (change-state! &lastresults "")
          (change-state! &loops (maxNewInputLoops))
          (py-call (logger.setup_logging (logConfigPath)))
          ))

(= (initKnowledge)
   (progn (log INFO "loop" "Initializing knowledge base")
          (if (== (embeddingprovider) OpenAI)
               (log INFO "loop" (py-call (rag.init_knowledge "OpenAI")))
               (log INFO "loop" (py-call (rag.init_knowledge "Local"))))))

(= (getContext)
   (string-safe (py-str ("PROMPT: " (getPrompt (provider))
                         (newline) "ROBOT BODY CONTEXT: " (py-call (robot.get_system))
                         " SKILLS: " (getSkills)
                         " OUTPUT_FORMAT: Up to 5 lines, do not wrap quotes around args, do not use variables:" (newline)
                         " toolName1 arg1" (newline)
                         " toolName2 arg2" (newline)
                         " toolName3 arg3" (newline)
                         " toolName4 arg4" (newline)
                         " toolName5 arg5" (newline)
                         " SAVE_PERMANENT_FILES_DIR: " (memoryDirectory)
                         " LAST_SKILL_USE_RESULTS: you must rectify ALERT_FAILED skill use cases " (last_chars (get-state &lastresults) (maxFeedback)) 
                         " HISTORY: " (getHistory) 
                         " TIME: " (get_time_as_string)))))

(= (HandleError $msg $cmd $sexpr)
   (case $sexpr (((Error $a $b) (let $new (append (get-state &error) (($msg $cmd)))
                                          (progn (change-state! &error $new) (ALERT_FAILED $a $b))))
                 ($else $sexpr))))

(= (omegaclaw)
   (omegaclaw 1))

; The whole agent: one tail-recursive tick, ~once per sleepInterval, forever.
; $k is the iteration number. Line at the very bottom recurses into the next tick.
(= (omegaclaw $k)
   (progn
     ; First tick only: boot. Every later tick: spend one unit of thinking budget.
     (if (== $k 1)
         (progn (initLoop)
                (applySecurityPolicy)
                (initMemory)
                (initKnowledge)
                (initChannels))
         (change-state! &loops (- (get-state &loops) 1)))

     ; Rebuild the full prompt from scratch this tick (persona + robot body +
     ; skills + history + last results + time).
     (let $prompt (getContext)
       (progn
         (log INFO "loop" (---------iteration $k))

         ; Poll the channel and decide whether a NEW message arrived. A new
         ; message refills the thinking budget (&loops) back to maxNewInputLoops.
         (let* (($msgrcv (string-safe (repr (receive))))
                ($msgnew (prog1 (and (> (string_length $msgrcv) 0) (!= $msgrcv (get-state &prevmsg)))
                                (if (> (string_length $msgrcv) 0) (change-state! &prevmsg $msgrcv) _)))
                ($msg (get-state &prevmsg))
                ($_ (if (and (> $k 1) $msgnew)
                        (change-state! &loops (maxNewInputLoops)) _)))

           (if (> (get-state &loops) 0)

               ; ===== budget remains: THINK this tick (this branch calls the LLM) =====
               (let* (($lastmessage (if $msgnew (HUMAN-MSG: $msg) (if (spamShield) " DO NOT RE-SEND OR SPAM!" "")))
                      ($_ (change-state! &nextWakeAt (+ (get_time) (wakeupInterval))))
                      ($_ (log INFO "loop" $lastmessage))
                      ($send (py-str ($prompt :-:-:-: $lastmessage)))
                      ($_ (log INFO "loop" (CHARS_SENT: (string_length $send) $send)))
                      ; 1. call the LLM  2. parse its text into command s-exprs
                      ($respi (py-call (lib_llm_ext.callProvider (provider) $send (maxOutputToken) (reasoningMode))))
                      ($resp (py-call (helper.balance_parentheses $respi)))
                      ($response (if (== "(" (first_char $resp))
                                     $resp
                                     (progn (log INFO "loop" $resp)
                                            (repr (REMEMBER:OUTPUT_NOTHING_ELSE_THAN: ((skill arg) ...))))))
                      ($sexpr (catch (sread $response)))
                      ($_ (change-state! &error ()))
                      ($mcerr (HandleError MULTI_COMMAND_FAILURE_NOTHING_WAS_DONE_PLEASE_CORRECT_PARENTHESES_AND_USE_QUOTES_AND_RETRY $response $sexpr))
                      ($_ (log INFO "loop" (RESPONSE: $sexpr)))
                      ; 3. run EACH command (superpose splits, collapse regathers),
                      ;    wrapping each as (COMMAND_RETURN: (command result)).
                      ($results (if (== $mcerr $sexpr)
                                    (RESULTS: (collapse (let $s (superpose $sexpr)
                                                          (COMMAND_RETURN: ($s (HandleError SINGLE_COMMAND_ERROR_NOTHING_WAS_DONE_PLEASE_FIX_AND_RETRY $s (catch (let $R (eval $s) (py-call (helper.normalize_string $R))))))))))
                                    (RESULTS: $mcerr)))
                      ($_ (log INFO "loop" (RESPONSE: $results))))
                 ; persist to history, and keep results for next tick's prompt
                 (progn (if (or $msgnew  (not (== $sexpr ()))) (addToHistory $msg $response $sexpr $msgnew) _)
                        (change-state! &lastresults (string-safe (repr $results)))))

               ; ===== no budget: IDLE. Self-wake once wakeupInterval has passed. =====
               (if (> (get_time) (get-state &nextWakeAt))
                   (change-state! &loops (+ 1 (maxWakeLoops))) _)))

         ; End of tick: sleep, cut choice points, gc, recurse into the next tick.
         (sleep (sleepInterval))
         (cut)
         (gc)
         (omegaclaw (+ 1 $k))))))
