// helper functions for verifying a ballot
// assumes all of Helios machinery is loaded
// and the translation functions from /jsi18n/ (gettext, interpolate)

function verify_ballot(election_raw_json, encrypted_vote_json, status_cb) {
    var overall_result = true;
    try {
	election = HELIOS.Election.fromJSONString(election_raw_json);
	var election_hash = election.get_hash();
	status_cb(interpolate(gettext("election fingerprint is %(hash)s"), {'hash': election_hash}, true));
	
	// display ballot fingerprint
	encrypted_vote = HELIOS.EncryptedVote.fromJSONObject(encrypted_vote_json, election);
	status_cb(interpolate(gettext("ballot tracker is %(hash)s"), {'hash': encrypted_vote.get_hash()}, true));
	
      // check the hash
      if (election_hash == encrypted_vote.election_hash) {
          status_cb(gettext("election fingerprint matches ballot"));
      } else {
          overall_result = false;
          status_cb(gettext("PROBLEM = election fingerprint does not match"));          
      }
      
      // display the ballot as it is claimed to be
      status_cb(gettext("Ballot Contents:"));
      _(election.questions).each(function(q, qnum) {
	      if (q.tally_type != "homomorphic") {
		  status_cb(gettext("WARNING: the tally type for this question is not homomorphic. Verification may fail because this verifier is only set up to handle homomorphic ballots."));
	      }
        
	      var answer_pretty_list = _(encrypted_vote.encrypted_answers[qnum].answer).map(function(aindex, anum) {
		      return q.answers[aindex];
		  });
	      status_cb(interpolate(gettext("Question #%(num)s - %(name)s : %(answers)s"), {'num': qnum+1, 'name': q.short_name, 'answers': answer_pretty_list.join(", ")}, true));
      });
      
      // verify the encryption
      if (encrypted_vote.verifyEncryption(election.questions, election.public_key)) {
          status_cb(gettext("Encryption Verified"));
      } else {
          overall_result = false;
          status_cb(gettext("PROBLEM = Encryption doesn't match."));
      }
      
      // verify the proofs
      if (encrypted_vote.verifyProofs(election.public_key, function(ea_num, choice_num, result) {
      })) {
          status_cb(gettext("Proofs ok."));
      } else {
          overall_result = false;
          status_cb(gettext("PROBLEM = Proofs don't work."));
      }
    } catch (e) {
      status_cb(gettext('problem parsing election or ballot data structures, malformed inputs:') + ' ' + e.toString());
      overall_result=false;
    }

    return overall_result;
}
