# Calculator
#
# Classes and methods to calculate fluxes

import numpy as np
from warnings import warn
from constants import BOUNDARY_CONDITIONS

class DiamondDifferenceCalculator1D(object):
	"""One-group, one-dimensional diamond difference solver
	
	Parameters:
	-----------
	quad:           Quadrature; 1-D angular quadrature to use
	mesh:           Mesh1D; 1-D mesh to solve on.
					Use one tailored to your problem.
	bcs:            tuple of ("left", "right")
	kguess:         float; initial guess for the eigenvalue
					[Default: 1.0]
	accelerator:    Accelerator, if one is desired
					[Default: None]
	"""
	def __init__(self, quad, mesh, kguess=1.0, accelerator=None):
		self.quad = quad
		self.mesh = mesh
		self.k = kguess
		self.accelerator = accelerator
		self.fission_source = self.mesh.calculate_fission_source()
		self.scatter_source = self.mesh.calculate_scatter_source()
		self._get_psi_left, self._get_psi_right = self.__set_bcs(mesh.bcs)
	
	
	def _l2norm(self, mg_new, mg_old):
		diff = 0.0
		for i in range(self.mesh.nx):
			for g in range(self.mesh.groups):
				# Calculate the fission source difference
				mg0 = mg_old[i, g]
				mg1 = mg_new[i, g]
				if mg1 != mg0:
					diff += ((mg1 - mg0)/mg1)**2
		return np.sqrt(diff/self.mesh.nx)
		
	
	def __set_bcs(self, bcs):
		lc, rc = bcs
		if "periodic" in bcs and lc != rc:
			errstr = "If one edge has a periodic boundary condition, " \
			         "both sides must."
			raise TypeError(errstr)
		
		if "periodic" in bcs:
			#raise NotImplementedError("periodic boundary condition")
			get_left = lambda n, g: self.mesh.psi[-1, n, g]
			get_right = lambda n, g: self.mesh.psi[0, n, g]
			return get_left, get_right
		
		if lc == "reflective":
			def get_left(n, g):
				m = self.quad.reflect_angle(n)
				return self.mesh.psi[0, m, g]
		elif lc == "vacuum":
			# No flux incoming from left
			get_left = lambda n, g: 0
		else:
			raise NotImplementedError(lc)
		
		if rc == "reflective":
			def get_right(n, g):
				m = self.quad.reflect_angle(n)
				return self.mesh.psi[-1, m, g]
		elif rc == "vacuum":
			# No flux incoming from right
			get_right = lambda n, g: 0
		else:
			raise NotImplementedError(rc)
		
		return get_left, get_right
	
	def transport_sweep(self, k):
		"""Perform one forward and one backward transport sweep.
		
		Returns:
		--------
		float; the L2 engineering norm after this sweep
		"""
		old_flux = np.array(self.mesh.flux[:, :])
		for g in range(self.mesh.groups):
			# Forward sweep
			for n in range(self.quad.N2):
				mu = abs(self.quad.mus[n])
				psi_in = self._get_psi_left(n, g)
				self.mesh.psi[0, n, g] = psi_in
				for i in range(self.mesh.nx):
					node = self.mesh.nodes[i]
					q = 0.5*self.fission_source[i, g]/k + 0.5*self.scatter_source[i, g]
					psi_out = psi_in*(2*mu - node.dx*node.sigma_tr[g]) + 2*node.dx*q
					psi_out /= 2*mu + node.dx*node.sigma_tr[g]
					
					self.mesh.psi[i+1, n, g] = psi_out
					psi_in = psi_out
			
			
			# Backward sweep
			for n in range(self.quad.N2, self.quad.N):
				mu = abs(self.quad.mus[n])
				psi_in = self._get_psi_right(n, g)
				self.mesh.psi[-1, n, g] = psi_in
				for i in range(self.mesh.nx):
					node = self.mesh.nodes[-1-i]
					q = 0.5*self.fission_source[i, g]/k + 0.5*self.scatter_source[i, g]
					psi_out = psi_in*(2*mu - node.dx*node.sigma_tr[g]) + 2*node.dx*q
					psi_out /= 2*mu + node.dx*node.sigma_tr[g]
					
					self.mesh.psi[-2-i, n, g] = psi_out
					psi_in = psi_out
				
				# Reconnect that last pesky boundary flux
				#self.mesh.psi[0, n, g] = psi_out
				m = self.quad.reflect_angle(n)
				self.mesh.psi[0, m, g] = self._get_psi_left(m, g)
				
			# Update the scalar flux using the Diamond Difference approximation
			#
			# Interior nodes
			for i in range(self.mesh.nx):
				flux_i = 0.0
				for n in range(self.quad.N):
					w = self.quad.weights[n]
					psi_plus = self.mesh.psi[i+1, n, g]
					psi_minus = self.mesh.psi[i, n, g]
					flux_i += w*(psi_plus + psi_minus)/2.0
				self.mesh.flux[i, g] = flux_i
		
		
		# Get the fission source and flux differences
		fluxdiff = 0.0
		fsdiff = 0.0
		fs_new = self.mesh.calculate_fission_source()
		
		for i in range(self.mesh.nx):
			for g in range(self.mesh.groups):
				# Calculate the fission source difference
				fs0 = self.fission_source[i, g]
				fs1 = fs_new[i, g]
				if fs1 != fs0:
					fsdiff += ((fs1 - fs0)/fs1)**2
				# Calculate the flux difference
				phi_i1 = self.mesh.flux[i, g]
				phi_i0 = old_flux[i, g]
				if phi_i1 != phi_i0:
					fluxdiff += ((phi_i1 - phi_i0)/phi_i1)**2
				
		rms_flux = np.sqrt(fluxdiff/self.mesh.nx)
		rms_fs = np.sqrt(fsdiff/self.mesh.nx)
		
		return fs_new, rms_fs, rms_flux
		
				
	def solve(self, eps, maxiter=1000):
		"""Solve on the mesh within tolerance
		
		Parameters:
		-----------
		eps:            float; tolerance to use
		maxiter:        int; the maximum number of iterations
						[Default: 100]
		
		Returns:
		--------
		flux:           numpy array of the scalar flux
		"""
		fsdiff = eps + 1
		kdiff = eps + 1
		outer_count = 0
		self.factor_by_iter = []
		self.rms_transport = []
		# Outer: converge the fission source
		while fsdiff > eps or kdiff > eps:
			print("kguess = {}".format(self.k))
			fs, fsdiff, fluxdiff = self.transport_sweep(self.k)
			# Inner: converge the flux
			# Find the relative difference in flux using the L2 engineering norm
			print("RMS (Transport): ", fluxdiff)
			self.rms_transport.append(fluxdiff)
			outer_count += 1
			
			if self.accelerator:
				#print("Flux before acceleration:")
				#print(self.mesh.flux)
				
				old_flux = np.array(self.mesh.flux)
				# Update the accleration method with the fine mesh fluxes
				self.accelerator.restrict_flux(self.mesh)
				# Converge the acceleration flux
				self.accelerator.solve(old_flux, self.fission_source, self.k, eps)
				# Update our fine mesh solution from the coarse mesh
				self.accelerator.prolong()
				self.factor_by_iter.append(abs(self.accelerator.coarse_mesh.factor - 1))
				fs = self.mesh.calculate_fission_source()
				
				#print("Flux after acceleration:")
				#print(self.mesh.flux)
				rms_new_flux = self._l2norm(self.mesh.flux, old_flux)
				print("RMS (CMFD):      ", rms_new_flux)
			
			# Now that flux has been converged, guess a new k
			# and update the fission source
			# Also find the relative difference in k
			ss = self.mesh.calculate_scatter_source()
			k_new = self.k*fs.sum()/self.fission_source.sum()
			kdiff = abs(k_new - self.k)/k_new
			print("k: {}\tkdiff: {}".format(k_new, kdiff))
			self.fission_source = fs
			self.scatter_source = ss
			self.k = k_new
			
			print("\n\n")
			
			if outer_count >= maxiter:
				errstr = "Solution did NOT converge after {} outer iterations; aborting."
				warn(errstr.format(outer_count))
				return False
			
		
		print("Solution converged after {} outer iterations.".format(outer_count))
		return True
		
